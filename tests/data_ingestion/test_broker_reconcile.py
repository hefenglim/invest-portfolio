"""The import gate. Every blocking check gets a case that FIRES and a case that does not.

Same fixture discipline as ``test_broker_adapter.py``: everything here is fictional. The
broker's own vocabulary appears verbatim because it is the thing under test.

A gate is only worth its runtime if it has been shown to fail. Several of these tests exist
because the corresponding check was written, believed, and then found to be vacuous on the
first hostile fixture — the reconciler's value is entirely in the cases below where a
plausible import is refused.
"""

from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.data_ingestion.broker import grouping, reconcile, schwab
from portfolio_dash.data_ingestion.broker.grouping import (
    DividendEvent,
    GroupedImport,
    SuppressedGroup,
    group_events,
)
from portfolio_dash.data_ingestion.broker.ir import EventKind, RawEvent
from portfolio_dash.data_ingestion.broker.reconcile import (
    ReconcileFailed,
    require_clean,
)
from portfolio_dash.data_ingestion.broker.reconcile import (
    reconcile as run_reconcile,
)

_HEADER = "Date,Action,Symbol,Description,Quantity,Price,Fees & Comm,Amount\n"


def _csv(*rows: str) -> str:
    return _HEADER + "".join(r if r.endswith("\n") else r + "\n" for r in rows)


def _parse(*rows: str, file: str = "export.csv") -> list[RawEvent]:
    return schwab.parse(_csv(*rows), source_file=file)


def _ev(
    kind: EventKind,
    *,
    day: str = "2026-01-05",
    symbol: str = "AAA",
    broker_symbol: str | None = None,
    qty: str = "0",
    amount: str = "0",
    price: str = "0",
    fees: str = "0",
    line: int = 1,
    file: str = "a.csv",
    description: str = "",
) -> RawEvent:
    return RawEvent(
        line_no=line, source_file=file, kind=kind,
        trade_date=date.fromisoformat(day), posted_date=date.fromisoformat(day),
        symbol=symbol, broker_symbol=symbol if broker_symbol is None else broker_symbol,
        quantity=Decimal(qty), amount=Decimal(amount), price=Decimal(price),
        fees=Decimal(fees), description=description,
    )


def _codes(report: reconcile.ReconcileReport, severity: str) -> set[str]:
    return {i.code for i in report.issues if i.severity == severity}


# ============================================================ the clean baseline


def _clean_events() -> list[RawEvent]:
    """A buy, a sell and a DRIP group that all reconcile — the control for every test below."""
    return _parse(
        '01/05/2026,Buy,AAA,"BOUGHT 10 AAA",10,$20.00,$1.00,-$201.00',
        '02/09/2026,Sell,AAA,"SOLD 4 AAA",4,$25.00,$1.00,$99.00',
        '03/11/2026,Cash Dividend,AAA,"CASH DIVIDEND",,,,$10.00',
        '03/11/2026,NRA Tax Adj,AAA,"W-8 WITHHOLDING",,,,-$3.00',
        '03/11/2026,Reinvest Shares,AAA,"REINVEST",0.28,$25.00,,-$7.00',
    )


def test_a_consistent_export_blocks_nothing() -> None:
    events = _clean_events()
    report = run_reconcile(events, group_events(events))
    assert report.blocking == ()
    assert report.ok
    require_clean(report)  # must not raise


def test_the_report_states_the_cash_and_the_share_delta_it_will_write() -> None:
    events = _clean_events()
    report = run_reconcile(events, group_events(events))
    # -201 bought, +99 sold, +10 gross, -3 withheld, -7 reinvested.
    assert report.cash_total == Decimal("-102.00")
    # +10 bought, -4 sold, +0.28 reinvested.
    assert report.share_deltas["AAA"] == Decimal("6.28")
    assert report.rows_in == 5


# ============================================================ row conservation


def test_a_lost_row_blocks() -> None:
    """``account_for``'s verdict is the gate's, not a warning printed beside it."""
    events = _clean_events()
    grouped = group_events(events)
    grouped.trades.pop()
    report = run_reconcile(events, grouped)
    assert "rows_lost" in _codes(report, "blocking")


def test_a_row_counted_twice_blocks() -> None:
    events = _clean_events()
    grouped = group_events(events)
    grouped.cash.append(grouped.trades[0])
    report = run_reconcile(events, grouped)
    assert "rows_lost" in _codes(report, "blocking")


# ============================================================ suppression re-measured


def test_a_dropped_group_that_does_not_net_to_zero_blocks() -> None:
    """The check that exists because ``suppress`` phase 1 ASSERTS its zeros rather than
    measuring them: a group claiming to cancel out, over rows that do not."""
    real = _ev(EventKind.BUY, symbol="AAA", qty="10", price="20", amount="-200", line=1)
    noise = _ev(EventKind.JOURNAL_INTERNAL, symbol="AAA", qty="10", amount="0", line=2)
    grouped = GroupedImport(
        suppressed=[
            SuppressedGroup(
                key=(date(2026, 1, 5), "AAA"),
                kinds=(EventKind.BUY, EventKind.JOURNAL_INTERNAL),
                refs=(real.ref, noise.ref),
                amount_sum=Decimal(0),   # the LIE the reconciler must not believe
                quantity_sum=Decimal(0),
            )
        ]
    )
    report = run_reconcile([real, noise], grouped)
    assert "suppressed_not_zero" in _codes(report, "blocking")
    assert "-200" in next(
        i.detail for i in report.blocking if i.code == "suppressed_not_zero"
    )


def test_a_dropped_group_zero_in_cash_but_not_in_shares_still_blocks() -> None:
    """The share dimension is the one that protects a split: two legs, no cash, 3-for-1."""
    out = _ev(EventKind.SPLIT, symbol="AAA", qty="-85", amount="0", line=1)
    into = _ev(EventKind.SPLIT, symbol="AAA", qty="255", amount="0", line=2)
    grouped = GroupedImport(
        suppressed=[
            SuppressedGroup(
                key=(date(2026, 1, 5), "AAA"),
                kinds=(EventKind.SPLIT,),
                refs=(out.ref, into.ref),
                amount_sum=Decimal(0),
                quantity_sum=Decimal(0),
            )
        ]
    )
    report = run_reconcile([out, into], grouped)
    detail = next(i.detail for i in report.blocking if i.code == "suppressed_not_zero")
    assert "quantity 170" in detail


def test_a_dropped_group_citing_an_unknown_row_blocks() -> None:
    real = _ev(EventKind.JOURNAL_INTERNAL, line=1)
    grouped = GroupedImport(
        suppressed=[
            SuppressedGroup(
                key=(date(2026, 1, 5), "AAA"),
                kinds=(EventKind.JOURNAL_INTERNAL,),
                refs=(real.ref, "ghost.csv:99"),
                amount_sum=Decimal(0),
                quantity_sum=Decimal(0),
            )
        ]
    )
    report = run_reconcile([real], grouped)
    assert "suppressed_ref_unknown" in _codes(report, "blocking")


# ============================================================ the broker's own arithmetic


@pytest.mark.parametrize(("row", "fires"), [
    ('01/05/2026,Buy,AAA,"B",10,$20.00,$1.00,-$201.00', False),
    ('01/05/2026,Buy,AAA,"B",10,$20.00,$1.00,-$199.00', True),   # fees added the wrong way
    ('01/05/2026,Buy,AAA,"B",10,$20.00,,$200.00', True),         # sign dropped
    ('01/05/2026,Sell,AAA,"S",10,$20.00,$1.00,$199.00', False),
    ('01/05/2026,Sell,AAA,"S",10,$20.00,$1.00,$201.00', True),
    ('01/05/2026,Sell,AAA,"S",10,$20.00,$1.00,$1990.00', True),  # a lost decimal point
])
def test_a_trade_must_reconcile_to_quantity_times_price(row: str, fires: bool) -> None:
    events = _parse(row)
    report = run_reconcile(events, group_events(events))
    assert ("priced_row_mismatch" in _codes(report, "blocking")) is fires


def test_an_unpriced_transfer_booked_as_a_buy_is_not_a_mismatch() -> None:
    """'Reconciles to zero' would be a vacuous pass, so a row with no price is skipped."""
    events = _parse('01/05/2026,Buy,AAA,"TRANSFER IN",10,,,')
    report = run_reconcile(events, group_events(events))
    assert _codes(report, "blocking") == set()


def test_a_priced_trade_that_moves_no_cash_is_a_free_position_and_blocks() -> None:
    """Observed once for real: a when-issued ADR re-badged regular-way, booked at 50 x $170
    for $0.00, with the cancelling row carrying no symbol to pair it with."""
    events = _parse('01/05/2026,Buy,AAA,"WHEN ISSUED",50,$170.00,,')
    report = run_reconcile(events, group_events(events))
    assert "priced_row_no_cash" in _codes(report, "blocking")
    assert "priced_row_mismatch" not in _codes(report, "blocking")


# ============================================================ distributions


def test_a_reinvestment_larger_than_its_payout_blocks() -> None:
    div = DividendEvent(
        trade_date=date(2026, 3, 11), symbol="AAA",
        gross=Decimal("10.00"), withholding=Decimal("3.00"),
        reinvest_shares=Decimal("4"), reinvest_price=Decimal("25.00"),  # $100 from a $7 net
        refs=(),
    )
    report = run_reconcile([], GroupedImport(dividends=[div]))
    assert "over_reinvested" in _codes(report, "blocking")


def test_a_one_cent_overdraw_is_broker_rounding_not_a_defect() -> None:
    """Measured on 225 real DRIP groups: exactly one overdraws, by exactly one cent."""
    div = DividendEvent(
        trade_date=date(2026, 3, 11), symbol="AAA",
        gross=Decimal("0.70"), withholding=Decimal("0.18"),
        reinvest_shares=Decimal("1"), reinvest_price=Decimal("0.53"),
        refs=(),
    )
    report = run_reconcile([], GroupedImport(dividends=[div]))
    assert "over_reinvested" not in _codes(report, "blocking")


def test_withholding_larger_than_the_gross_blocks() -> None:
    div = DividendEvent(
        trade_date=date(2026, 3, 11), symbol="AAA",
        gross=Decimal("10.00"), withholding=Decimal("30.00"),
        reinvest_shares=None, reinvest_price=None, refs=(),
    )
    report = run_reconcile([], GroupedImport(dividends=[div]))
    assert "withholding_exceeds_gross" in _codes(report, "blocking")


# ============================================================ conservation of money & shares


def test_money_invented_by_the_fold_blocks() -> None:
    events = _clean_events()
    grouped = group_events(events)
    fat = grouped.dividends[0]
    grouped.dividends[0] = DividendEvent(
        trade_date=fat.trade_date, symbol=fat.symbol,
        gross=Decimal("1000.00"), withholding=fat.withholding,
        reinvest_shares=fat.reinvest_shares, reinvest_price=fat.reinvest_price,
        refs=fat.refs,
    )
    report = run_reconcile(events, grouped)
    assert "cash_not_conserved" in _codes(report, "blocking")


def test_shares_invented_by_the_fold_block() -> None:
    events = _clean_events()
    grouped = group_events(events)
    d = grouped.dividends[0]
    grouped.dividends[0] = DividendEvent(
        trade_date=d.trade_date, symbol=d.symbol, gross=d.gross, withholding=d.withholding,
        reinvest_shares=Decimal("999"), reinvest_price=d.reinvest_price, refs=d.refs,
    )
    report = run_reconcile(events, grouped)
    assert "shares_not_conserved" in _codes(report, "blocking")


def test_the_derived_share_rule_is_the_baseline_not_the_printed_quantity() -> None:
    """The printed 0.27 disagrees with 7.00 / 25.00; conservation must accept the DERIVED
    0.28, or the check would forbid the rule it is supposed to protect."""
    events = _parse(
        '03/11/2026,Cash Dividend,AAA,"CASH DIVIDEND",,,,$10.00',
        '03/11/2026,NRA Tax Adj,AAA,"W-8 WITHHOLDING",,,,-$3.00',
        '03/11/2026,Reinvest Shares,AAA,"REINVEST",0.27,$25.00,,-$7.00',
    )
    report = run_reconcile(events, group_events(events))
    assert _codes(report, "blocking") == set()
    assert report.share_deltas["AAA"] == Decimal("0.28")


# ============================================================ CUSIPs


def test_a_cusip_the_file_never_names_blocks_and_says_which_company() -> None:
    events = _parse(
        '10/06/2025,Reverse Split,22222B222,"WIDGET INC. REVERSE SPLIT EFF: 10/06/25",-200,,,'
    )
    report = run_reconcile(events, group_events(events))
    issue = next(i for i in report.blocking if i.code == "cusip_unresolved")
    assert "22222B222" in issue.detail
    assert "WIDGET INC" in issue.detail          # the owner can answer it from the message
    # and it must NOT also be pushed at the owner as a missing opening position
    assert "prehistory_position" not in _codes(report, "advisory")


def test_a_cusip_the_file_does_name_needs_no_alias() -> None:
    events = _parse(
        '04/12/2021,Buy,11111A111,"TDA TRAN - Bought 5 (AAA) @4.8800",5,$4.88,,-$24.40'
    )
    assert events[0].symbol == "AAA"
    assert events[0].broker_symbol == "11111A111"
    report = run_reconcile(events, group_events(events))
    assert "cusip_unresolved" not in _codes(report, "blocking")


def test_an_alias_is_inferred_from_the_rows_that_do_name_the_ticker() -> None:
    """The whole point: 27 rows name the ticker and one does not, and importing them as two
    instruments splits the position's cost basis in half."""
    events = _parse(
        '04/12/2021,Buy,11111A111,"TDA TRAN - Bought 5 (AAA) @4.8800",5,$4.88,,-$24.40',
        '05/26/2021,Journaled Shares,11111A111,'
        '"TDA TRAN - MANDATORY REVERSE SPLIT (11111A111)",-100,,,',
    )
    resolved, ambiguous = grouping.infer_cusip_aliases(events)
    assert resolved == {"11111A111": "AAA"}
    assert ambiguous == {}
    applied = grouping.apply_aliases(events, resolved)
    assert [e.symbol for e in applied] == ["AAA", "AAA"]


def test_a_cusip_naming_two_tickers_is_ambiguous_and_left_alone() -> None:
    a = _ev(EventKind.BUY, symbol="AAA", broker_symbol="11111A111", line=1)
    b = _ev(EventKind.BUY, symbol="BBB", broker_symbol="11111A111", line=2)
    resolved, ambiguous = grouping.infer_cusip_aliases([a, b])
    assert resolved == {}
    assert ambiguous == {"11111A111": {"AAA", "BBB"}}


def test_apply_aliases_leaves_untouched_events_identical() -> None:
    events = _clean_events()
    assert grouping.apply_aliases(events, {}) is events
    assert grouping.apply_aliases(events, {"ZZZ": "YYY"}) == events


# ============================================================ pre-history


def test_shares_arriving_by_corporate_action_are_not_a_missing_opening_position() -> None:
    """The defect this fixes fabricated six opening positions on a real export — a spinoff, a
    10-for-1 split, two SPAC tickers and two CUSIP legs — each inviting the owner to invent an
    opening cost for shares the file already explains."""
    events = _parse(
        '04/01/2024,Journaled Shares,NEW,"TDA TRAN - SPIN OFF (NEW)",16,,,',
        '06/03/2024,Sell,NEW,"SOLD 16 NEW",16,$10.00,,$160.00',
    )
    assert grouping.prehistory_shares(events) == {}
    report = run_reconcile(events, group_events(events))
    assert "prehistory_position" not in _codes(report, "advisory")


def test_an_oversell_still_reports_a_missing_opening_position() -> None:
    events = _parse(
        '01/05/2026,Buy,AAA,"B",10,$20.00,,-$200.00',
        '02/09/2026,Sell,AAA,"S",25,$20.00,,$500.00',
    )
    assert grouping.prehistory_shares(events) == {"AAA": Decimal(15)}
    report = run_reconcile(events, group_events(events))
    issue = next(i for i in report.advisory if i.code == "prehistory_position")
    assert "15 shares" in issue.detail


def test_a_history_that_opens_with_a_reinvest_reports_an_unknown_quantity() -> None:
    events = _parse('03/11/2026,Reinvest Shares,AAA,"REINVEST",0.28,$25.00,,-$7.00')
    report = run_reconcile(events, group_events(events))
    issue = next(i for i in report.advisory if i.code == "prehistory_position")
    assert "unknown quantity" in issue.detail


# ============================================================ severity is not a matter of taste


def test_advisories_do_not_refuse_the_import() -> None:
    """An export legitimately contains option legs and pre-history. A gate that rejected those
    would reject every real statement and be switched off within a week."""
    events = _parse(
        '08/13/2021,Sell to Open,AAA 01/21/2022 30.00 C,"SOLD OPTION",1,$2.00,,$200.00',
        '03/11/2026,Reinvest Shares,AAA,"REINVEST",0.28,$25.00,,-$7.00',
    )
    report = run_reconcile(events, group_events(events))
    assert {"option_row_unsupported", "prehistory_position"} <= _codes(report, "advisory")
    assert report.ok
    require_clean(report)  # must not raise


def test_every_skipped_row_is_named_individually_not_summarised() -> None:
    events = _parse(
        '08/13/2021,Sell to Open,AAA 01/21/2022 30.00 C,"A",1,$2.00,,$200.00',
        '08/14/2021,Sell to Open,BBB 01/21/2022 30.00 C,"B",1,$2.00,,$200.00',
    )
    report = run_reconcile(events, group_events(events))
    options = [i for i in report.advisory if i.code == "option_row_unsupported"]
    assert len(options) == 2
    assert {i.refs for i in options} == {("export.csv:2",), ("export.csv:3",)}


def test_require_clean_raises_and_carries_the_report() -> None:
    events = _parse('01/05/2026,Buy,AAA,"B",10,$20.00,,$200.00')
    report = run_reconcile(events, group_events(events))
    with pytest.raises(ReconcileFailed) as excinfo:
        require_clean(report)
    assert excinfo.value.report is report
    assert "priced_row_mismatch" in str(excinfo.value)


def test_counts_summarise_without_naming_anything() -> None:
    events = _parse(
        '08/13/2021,Sell to Open,AAA 01/21/2022 30.00 C,"A",1,$2.00,,$200.00',
        '01/05/2026,Buy,BBB,"B",10,$20.00,,$200.00',
    )
    report = run_reconcile(events, group_events(events))
    assert report.counts()["priced_row_mismatch"] == 1
    assert report.counts()["option_row_unsupported"] == 1


# ============================================================ the committed corpus


def test_the_synthetic_corpus_reconciles(corpus_events: list[RawEvent]) -> None:
    """B5's committed fixture is the regression corpus: it must stay importable, and its
    advisories must stay the ones it was built to contain."""
    report = run_reconcile(corpus_events, group_events(corpus_events))
    assert report.blocking == (), [i.detail for i in report.blocking]
    require_clean(report)


@pytest.fixture
def corpus_events() -> list[RawEvent]:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "tests" / "golden" / "broker"
    events: list[RawEvent] = []
    for path in sorted(root.glob("schwab_20*.csv")):
        events.extend(
            schwab.parse(
                path.open(encoding="utf-8", newline="").read(), source_file=path.name
            )
        )
    return events
