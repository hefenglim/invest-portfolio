"""The broker adapter: classification, parsing, and the folds.

Every fixture here is FICTIONAL. The rules were derived from a real export, but that export
is the owner's personal financial data and is git-ignored; what is committed is the code and
a synthetic exercise of it. The broker's own vocabulary strings ("TDA TRAN - W-8
WITHHOLDING") are the broker's, not the owner's, so they appear verbatim — they are the
thing under test.

The load-bearing tests are the ones where a plausible simpler implementation destroys data:
a 1-for-1 ticker exchange surviving suppression, a cancel pair matching across a date
boundary, and a reinvest share count derived from amount/price rather than read off the row.
"""

import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from portfolio_dash.data_ingestion.broker import grouping, registry, schwab
from portfolio_dash.data_ingestion.broker.ir import EventKind, RawEvent, UnmappedRow

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
    qty: str = "0",
    amount: str = "0",
    price: str = "0",
    line: int = 1,
    file: str = "a.csv",
) -> RawEvent:
    return RawEvent(
        line_no=line, source_file=file, kind=kind,
        trade_date=date.fromisoformat(day), posted_date=date.fromisoformat(day),
        symbol=symbol, quantity=Decimal(qty), amount=Decimal(amount), price=Decimal(price),
    )


# ============================================================ classification


@pytest.mark.parametrize(("action", "description", "kind"), [
    ("Buy", "", EventKind.BUY),
    ("Buy", "BUY TO COVER SHORT POSITION", EventKind.BUY_COVER),
    ("Sell", "", EventKind.SELL),
    ("Sell Short", "", EventKind.SELL_SHORT),
    ("Reinvest Shares", "", EventKind.DRIP_BUY),
    ("Cash Dividend", "", EventKind.DIVIDEND),
    ("Qual Div Reinvest", "", EventKind.DIVIDEND),
    ("NRA Tax Adj", "", EventKind.WITHHOLDING_TAX),
    ("Foreign Tax Reclaim", "", EventKind.WITHHOLDING_TAX),
    ("Long Term Cap Gain", "", EventKind.CAPGAIN_DIST),
    ("Wire Received", "", EventKind.DEPOSIT),
    ("Credit Interest", "", EventKind.INTEREST_INCOME),
    ("Margin Interest", "", EventKind.INTEREST_EXPENSE),
    ("ADR Mgmt Fee", "", EventKind.FEE),
    ("Cash In Lieu", "", EventKind.CASH_IN_LIEU),
    ("Stock Merger", "", EventKind.NAME_CHANGE),
    ("Reverse Split", "", EventKind.REVERSE_SPLIT),
    ("Sell to Open", "", EventKind.OPT_SELL_OPEN),
    ("Expired", "", EventKind.OPT_EXPIRED),
    ("Internal Transfer", "", EventKind.JOURNAL_INTERNAL),
    ("Cancel Buy", "", EventKind.CANCELLED),
    ("Reinvestment Adj", "", EventKind.REINVEST_ADJ),
])
def test_action_keyed_rules(action: str, description: str, kind: EventKind) -> None:
    assert schwab.classify(action, description) is kind


@pytest.mark.parametrize(("description", "kind"), [
    ("TDA TRAN - W-8 WITHHOLDING (AAA)", EventKind.WITHHOLDING_TAX),
    ("TDA TRAN - INTERNAL TRANSFER BETWEEN ACCOUNTS OR ACCOUNT TYPES (AAA)",
     EventKind.JOURNAL_INTERNAL),
    ("TDA TRAN - INTRA-ACCOUNT TRANSFER", EventKind.JOURNAL_INTERNAL),
    ("TDA TRAN - TRANSFER OF SECURITY OR OPTION OUT (AAA)", EventKind.TRANSFER_OUT),
    ("TDA TRAN - CASH MOVEMENT OF OUTGOING ACCOUNT TRANSFER", EventKind.CASH_SWEEP),
    ("TDA TRAN - MARK TO THE MARKET", EventKind.MARK_TO_MARKET),
    ("TDA TRAN - MANDATORY REORGANIZATION FEE (AAA)", EventKind.FEE),
    ("TDA TRAN - MANDATORY REVERSE SPLIT (AAA)", EventKind.REVERSE_SPLIT),
    ("TDA TRAN - MANDATORY - EXCHANGE (AAA)", EventKind.EXCHANGE),
    ("TDA TRAN - STOCK SPLIT (AAA)", EventKind.SPLIT),
    ("TDA TRAN - NON-TAXABLE SPIN OFF/LIQUIDATION DISTRIBUTION (AAA)", EventKind.SPINOFF),
    ("TDA TRAN - OPTION ODD SPLIT REORGANIZATION (AAA)", EventKind.OPT_ADJUST),
    ("TDA TRAN - OPTION POSITION CHANGE (AAA)", EventKind.OPT_ADJUST),
    ("TDA TRAN - REMOVAL OF OPTION DUE TO EXPIRATION 1A1NI0)", EventKind.OPT_ADJUST),
])
def test_the_overloaded_action_is_keyed_on_its_description(
    description: str, kind: EventKind
) -> None:
    """All 14 meanings of one action value. The two biggest sit on opposite sides of the
    keep/drop line, so an action-only rule deletes real tax rows or keeps phantom ones."""
    assert schwab.classify("Journaled Shares", description) is kind


def test_reorganization_fee_wins_over_the_other_mandatory_patterns() -> None:
    """Order inside the table is load-bearing: three descriptions start MANDATORY and one
    of them is a fee, not a corporate action."""
    fee = "TDA TRAN - MANDATORY REORGANIZATION FEE (AAA)"
    assert schwab.classify("Journaled Shares", fee) is EventKind.FEE


def test_an_unmapped_row_raises_and_names_the_line() -> None:
    """Rule 7. A catch-all default is the same defect wearing a name: the ground-truth
    build this was lifted from ended with ``return OPT_ADJUST``, so any new description the
    broker invented would have been booked as an option adjustment and disappeared."""
    with pytest.raises(UnmappedRow) as excinfo:
        _parse("01/05/2026,Journaled Shares,AAA,TDA TRAN - SOMETHING NEW (AAA),1,,,0.00")
    assert excinfo.value.line_no == 2
    assert excinfo.value.source_file == "export.csv"
    assert "SOMETHING NEW" in str(excinfo.value)


def test_the_overloaded_action_has_no_default_rule() -> None:
    assert schwab.classify("Journaled Shares", "anything unrecognised") is None


def test_an_unknown_action_raises_too() -> None:
    with pytest.raises(UnmappedRow):
        _parse("01/05/2026,Teleported Shares,AAA,who knows,1,,,0.00")


# ============================================================ parsing


def test_a_dual_date_resolves_to_the_TRADE_date() -> None:
    """``post as of trade``. Measured on a real export: the printed-first date was 3 days
    AFTER the ``as of`` date in every dual-date row, so it is the broker's processing date.
    Taking it would mis-date those rows by the settlement lag, silently."""
    trade, posted = schwab.parse_dates("07/16/2025 as of 07/13/2025")
    assert trade == date(2025, 7, 13)
    assert posted == date(2025, 7, 16)


def test_a_single_date_is_both() -> None:
    assert schwab.parse_dates("07/16/2025") == (date(2025, 7, 16), date(2025, 7, 16))


@pytest.mark.parametrize(("raw", "want"), [
    ("-$1,234.56", "-1234.56"), ("$0.01", "0.01"), ("", "0"), ("1234.5", "1234.5"),
    ("($5.00)", "-5.00"),
])
def test_money_parses_exactly(raw: str, want: str) -> None:
    """Decimal from the string, never through float — the broker states cents."""
    assert schwab.parse_money(raw) == Decimal(want)


def test_a_symbol_is_recovered_from_the_description() -> None:
    """About a quarter of the assessed export's rows name the ticker only in the text."""
    assert schwab.recover_symbol("", "TDA TRAN - W-8 WITHHOLDING (BBB)") == "BBB"


def test_a_row_with_no_symbol_does_not_acquire_one() -> None:
    """Interest and wires legitimately have none; guessing at the first capitalised word
    would attach real money to an instrument that had nothing to do with it."""
    assert schwab.recover_symbol("", "CREDIT INTEREST FOR THE PERIOD") == ""


def test_an_alias_is_supplied_not_hard_coded() -> None:
    """A statement's CUSIPs are the owner's holdings; this file is committed. So the map is
    an input (D27: the program is committed, the data is not)."""
    assert schwab.recover_symbol("123456789", "", {"123456789": "CCC"}) == "CCC"


def test_an_option_leg_keeps_its_contract_out_of_symbol() -> None:
    """``shared/symbol_format.py`` rejects the shape and an option is not an instrument this
    ledger can register — so it must not arrive as one."""
    [event] = _parse("01/05/2026,Sell to Open,AAA 01/16/2026 50.00 P,SELL,1,2.00,0.65,134.35")
    assert event.symbol == ""
    assert event.option_symbol == "AAA 01/16/2026 50.00 P"
    assert event.is_option


def test_the_header_is_located_not_assumed() -> None:
    """Broker exports carry preamble lines; ``line_no`` stays the line in the ORIGINAL file
    because that is what a diagnostic has to cite."""
    text = ('"Transactions for account XXX"\n\n' + _HEADER
            + "01/05/2026,Buy,AAA,BUY,10,5.00,0.00,-50.00\n")
    [event] = schwab.parse(text, source_file="x.csv")
    assert event.line_no == 4


def test_a_file_without_the_header_is_refused() -> None:
    with pytest.raises(ValueError, match="no Schwab header row"):
        schwab.parse("a,b,c\n1,2,3\n", source_file="x.csv")


def test_refs_are_unique_across_files() -> None:
    """``line_no`` alone is not unique — merging five overlapping exports is the normal
    path, and keying on the bare number conflates line 2 of each."""
    a = _parse("01/05/2026,Buy,AAA,BUY,10,5.00,0.00,-50.00", file="1.csv")[0]
    b = _parse("01/06/2026,Buy,AAA,BUY,10,5.00,0.00,-50.00", file="2.csv")[0]
    assert a.line_no == b.line_no == 2
    assert a.ref != b.ref


# ============================================================ suppression


def test_a_journal_pair_is_dropped() -> None:
    events = [
        _ev(EventKind.JOURNAL_INTERNAL, qty="100", line=2),
        _ev(EventKind.JOURNAL_INTERNAL, qty="-100", line=3),
    ]
    kept, dropped, vetoed = grouping.suppress(events)
    assert kept == [] and vetoed == []
    assert len(dropped) == 1 and len(dropped[0].refs) == 2


def test_a_ONE_FOR_ONE_EXCHANGE_SURVIVES() -> None:
    """THE case that broke the rule as first written.

    A 1-for-1 ticker exchange nets zero in BOTH dimensions — shares out equal shares in, no
    cash on either leg — so it is arithmetically indistinguishable from an internal journal.
    Measured on a real export: grouping by DATE alone dropped 8 real corporate-action rows
    this way. Only the ``(date, symbol)`` key separates them, because the one thing that is
    different is that it is not the same security moving.
    """
    events = [
        _ev(EventKind.EXCHANGE, symbol="OLD", qty="-100", line=2),
        _ev(EventKind.EXCHANGE, symbol="NEW", qty="100", line=3),
        # ...and a genuine same-day journal, which MUST still be dropped.
        _ev(EventKind.JOURNAL_INTERNAL, symbol="ZZZ", qty="50", line=4),
        _ev(EventKind.JOURNAL_INTERNAL, symbol="ZZZ", qty="-50", line=5),
    ]
    kept, dropped, _ = grouping.suppress(events)
    assert {e.symbol for e in kept} == {"OLD", "NEW"}
    assert len(dropped) == 1


def test_a_forward_split_survives_the_quantity_term() -> None:
    """F-27's half: a split's legs are UNEQUAL, so summing quantity protects it where an
    amount-only check (both legs $0) would have dropped it."""
    events = [
        _ev(EventKind.SPLIT, qty="-85", line=2),
        _ev(EventKind.SPLIT, qty="255", line=3),
    ]
    kept, dropped, _ = grouping.suppress(events)
    assert len(kept) == 2 and dropped == []


def test_a_cancel_takes_its_original_with_it_across_a_date_boundary() -> None:
    """Rule 4. A cancel is often dated after the order it cancels — a same-day-only match
    leaves the cancelled trade in the ledger, i.e. a trade that never happened."""
    events = [
        _ev(EventKind.BUY, day="2026-01-05", qty="10", amount="-50", line=2),
        _ev(EventKind.CANCELLED, day="2026-01-06", qty="10", amount="50", line=3),
    ]
    kept, dropped, vetoed = grouping.suppress(events)
    assert kept == [] and vetoed == []
    assert len(dropped[0].refs) == 2


def test_a_zero_cash_journal_never_adopts_a_kept_row_as_its_partner() -> None:
    """The reversal match degenerates on a zero-cash row (``0 == -0`` matches anything), so
    it is restricted to rows that move cash. Without that restriction a journal drags a
    corporate-action leg out of the ledger with it — measured, twice, on the real export."""
    events = [
        _ev(EventKind.JOURNAL_INTERNAL, symbol="AAA", qty="100", line=2),
        _ev(EventKind.EXCHANGE, symbol="AAA", qty="100", line=3),
    ]
    kept, dropped, vetoed = grouping.suppress(events)
    assert dropped == []
    assert {e.kind for e in kept} == {EventKind.JOURNAL_INTERNAL, EventKind.EXCHANGE}
    assert len(vetoed) == 1  # the lone journal is reported, not silently kept


def test_a_group_that_does_not_net_to_zero_is_VETOED_not_dropped() -> None:
    """Classification says noise; arithmetic disagrees. The rows stay and the finding is
    reported — that combination is the one a human has to look at."""
    events = [
        _ev(EventKind.JOURNAL_INTERNAL, qty="100", amount="-10", line=2),
        _ev(EventKind.JOURNAL_INTERNAL, qty="-100", amount="0", line=3),
    ]
    kept, dropped, vetoed = grouping.suppress(events)
    assert dropped == [] and len(kept) == 2
    assert vetoed[0].amount_sum == Decimal("-10")
    assert "do not net to zero" in vetoed[0].reason


# ============================================================ dividends


def test_a_drip_triple_folds_into_one_dividend() -> None:
    events = [
        _ev(EventKind.DIVIDEND, amount="100", line=2),
        _ev(EventKind.WITHHOLDING_TAX, amount="-30", line=3),
        _ev(EventKind.DRIP_BUY, amount="-70", price="7", qty="10", line=4),
    ]
    [folded], remaining = grouping.fold_dividends(events)
    assert remaining == []
    assert folded.gross == Decimal("100")
    assert folded.withholding == Decimal("30")
    assert folded.net == Decimal("70")
    assert folded.is_drip and folded.reinvest_shares == Decimal("10")
    assert len(folded.refs) == 3


def test_reinvest_shares_come_from_amount_over_price_not_the_column() -> None:
    """Measured: 125 of 227 reinvest rows fail ``quantity x price == amount``, and in every
    case the printed quantity is the rounded view of the other two. Amount and price are
    what the broker asserts; trusting the display drifts the share count away from the
    statement in the same direction for a whole DRIP history."""
    events = [
        _ev(EventKind.DIVIDEND, amount="100", line=2),
        # printed 3.3333 shares, but 100/30 is 3.333... — the row's own quantity is rounded.
        _ev(EventKind.DRIP_BUY, amount="-100", price="30", qty="3.3333", line=3),
    ]
    [folded], _ = grouping.fold_dividends(events)
    assert folded.reinvest_shares == Decimal("100") / Decimal("30")
    assert folded.reinvest_shares != Decimal("3.3333")


def test_a_cash_dividend_folds_without_a_reinvestment() -> None:
    events = [
        _ev(EventKind.DIVIDEND, amount="100", line=2),
        _ev(EventKind.WITHHOLDING_TAX, amount="-30", line=3),
    ]
    [folded], _ = grouping.fold_dividends(events)
    assert not folded.is_drip and folded.reinvest_shares is None
    assert folded.net == Decimal("70")


# ============================================================ ratios & pre-history


@pytest.mark.parametrize(("out", "into", "want"), [
    ("85", "255", (3, 1)),      # 3-for-1 forward split
    ("100", "100", (1, 1)),     # 1-for-1 ticker exchange
    ("700", "200", (2, 7)),     # 2-for-7 merger
    ("100", "10", (1, 10)),     # 1-for-10 reverse split
])
def test_a_ratio_is_recovered_as_INTEGER_terms(out: str, into: str, want: tuple[int, int]) -> None:
    """The export states the share delta, never the ratio. D14 rejects a decimal ratio: a
    rounded quotient (0.2857 for 2-for-7) re-creates the 賣超 cascade P0 exists to prevent."""
    assert grouping.derive_ratio(Decimal(out), Decimal(into)) == want


def test_a_ratio_cannot_be_invented_from_a_zero_leg() -> None:
    with pytest.raises(ValueError, match="cannot derive a ratio"):
        grouping.derive_ratio(Decimal(0), Decimal(100))


def test_prehistory_detects_a_position_that_goes_negative() -> None:
    """Hard: the replay trips the sticky 賣超 guard and DISCARDS the cost basis for good."""
    events = [
        _ev(EventKind.BUY, symbol="AAA", qty="10", day="2026-01-05", line=2),
        _ev(EventKind.SELL, symbol="AAA", qty="30", day="2026-01-06", line=3),
    ]
    assert grouping.prehistory_shares(events) == {"AAA": Decimal("20")}


def test_prehistory_uses_the_running_MINIMUM_not_the_final_balance() -> None:
    """A back-dated dip a later buy covers is invisible to a net-only check — the same
    date-aware reasoning the ledger's own oversell guard needed."""
    events = [
        _ev(EventKind.SELL, symbol="AAA", qty="30", day="2026-01-05", line=2),
        _ev(EventKind.BUY, symbol="AAA", qty="100", day="2026-01-06", line=3),
    ]
    assert grouping.prehistory_shares(events)["AAA"] == Decimal("30")


def test_prehistory_also_flags_a_position_that_merely_OPENS_with_a_reinvest() -> None:
    """Soft, and the reason the two detectors are UNIONED: this one never goes negative, so
    the balance detector cannot see it, and its basis would be silently wrong. A DRIP
    implies a holding. The share count is unknowable from the file, so it is 0 — a marker
    that a cost is required, not an invented quantity."""
    events = [_ev(EventKind.DRIP_BUY, symbol="AAA", qty="1", day="2026-01-05", line=2)]
    assert grouping.prehistory_shares(events) == {"AAA": Decimal("0")}


def test_a_clean_position_is_not_flagged() -> None:
    events = [
        _ev(EventKind.BUY, symbol="AAA", qty="30", day="2026-01-05", line=2),
        _ev(EventKind.SELL, symbol="AAA", qty="10", day="2026-01-06", line=3),
    ]
    assert grouping.prehistory_shares(events) == {}


# ============================================================ conservation & routing


def test_every_row_is_accounted_for() -> None:
    """A converter that can lose a row quietly is one whose output cannot be trusted to be
    complete — which is the whole reason this ledger did not already have a broker importer.
    Line refs, not counts: a count balances while two rows swap places."""
    events = [
        _ev(EventKind.BUY, qty="10", amount="-50", line=2),
        _ev(EventKind.DIVIDEND, amount="5", line=3),
        _ev(EventKind.JOURNAL_INTERNAL, symbol="ZZZ", qty="1", line=4),
        _ev(EventKind.JOURNAL_INTERNAL, symbol="ZZZ", qty="-1", line=5),
        _ev(EventKind.SPLIT, symbol="SSS", qty="-1", line=6),
        _ev(EventKind.SPLIT, symbol="SSS", qty="2", line=7),
    ]
    grouped = grouping.group_events(events)
    grouping.account_for(events, grouped)      # raises on a loss or a double count
    assert len(grouped.actions) == 2
    assert len(grouped.trades) == 1


def test_a_vetoed_row_is_listed_as_unrouted_not_discarded() -> None:
    """The first version of ``group_events`` fell off the end of an if/elif chain and lost
    two rows of a real 1,375-row export without a word."""
    events = [_ev(EventKind.JOURNAL_INTERNAL, qty="1", amount="-6.21", line=2)]
    grouped = grouping.group_events(events)
    grouping.account_for(events, grouped)
    assert len(grouped.vetoed) == 1
    assert [e.ref for e in grouped.unrouted] == ["a.csv:2"]


def test_options_are_recognised_but_routed_nowhere() -> None:
    """Recognised is not supported: rule 7 must not fire on a statement that legitimately
    contains options, and the ledger must not pretend it booked them."""
    [event] = _parse("01/05/2026,Sell to Open,AAA 01/16/2026 50.00 P,SELL,1,2.00,0.65,134.35")
    grouped = grouping.group_events([event])
    assert grouped.options == [event]
    assert grouped.trades == [] and grouped.cash == []


def test_an_overlap_duplicate_is_reported_not_dropped() -> None:
    """Consecutive broker exports overlap, so one event can be printed in both. Two deposits
    of the same amount on the same day is also a real thing, so the pair is reported for a
    human rather than silently resolved."""
    a = _ev(EventKind.DEPOSIT, symbol="", amount="100", line=2, file="1.csv")
    b = _ev(EventKind.DEPOSIT, symbol="", amount="100", line=9, file="2.csv")
    assert grouping.overlap_duplicates([a, b]) == [(a, b)]
    # ...the same row twice within ONE file is not an overlap; it is two events.
    c = _ev(EventKind.DEPOSIT, symbol="", amount="100", line=3, file="1.csv")
    assert grouping.overlap_duplicates([a, c]) == []


# ============================================================ registry


def test_the_registry_resolves_schwab() -> None:
    assert registry.get_adapter("schwab") is schwab.parse
    assert "schwab" in registry.BROKER_IDS


def test_an_unknown_broker_is_refused_not_guessed() -> None:
    """Exports differ in columns, dates, signs and the meaning of their codes, so a generic
    parse of an unrecognised file yields a plausible, silently wrong ledger."""
    with pytest.raises(KeyError, match="unknown broker"):
        registry.get_adapter("definitely-not-a-broker")


# ============================================================ the synthetic corpus
#
# The rules above were derived from a REAL broker export that is git-ignored and can never
# be a CI fixture. These read a committed corpus in the broker's exact wire shape whose
# tickers are invented and amounts re-coded (scripts/gen_broker_corpus.py) — the same split
# D27 drew for the corporate-action acceptance gate, applied to the input side.

_REPO = Path(__file__).resolve().parents[2]
_CORPUS = _REPO / "tests" / "golden" / "broker"


def _corpus(*names: str) -> list[RawEvent]:
    return [
        e
        for name in names
        for e in registry.parse_export(
            "schwab", (_CORPUS / name).read_text(encoding="utf-8"), source_file=name
        )
    ]


def test_the_committed_corpus_matches_its_generator() -> None:
    """No RNG, all literal constants — so a re-run is byte-identical and any diff in the
    corpus is a real change someone made on purpose."""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(_REPO / "scripts" / "gen_broker_corpus.py"),
         "--check"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_corpus_parses_and_conserves_every_row() -> None:
    events = _corpus("schwab_2024.csv", "schwab_2025.csv")
    grouped = grouping.group_events(events)
    grouping.account_for(events, grouped)     # raises on a loss or a double count
    assert len(events) == 46
    assert grouped.vetoed == [] and grouped.unrouted == []


def test_the_corpus_keeps_every_corporate_action() -> None:
    """The whole point. Five symbols' worth of actions survive suppression, including the
    1-for-1 exchange that nets zero in both dimensions."""
    events = _corpus("schwab_2024.csv", "schwab_2025.csv")
    grouped = grouping.group_events(events)
    assert sorted({e.symbol for e in grouped.actions}) == [
        "BETA", "GAMM", "NEWX", "OLDX", "SPIN"
    ]
    kinds = {e.kind for e in grouped.actions}
    assert EventKind.SPLIT in kinds
    assert EventKind.REVERSE_SPLIT in kinds
    assert EventKind.EXCHANGE in kinds
    assert EventKind.SPINOFF in kinds


def test_the_corpus_drops_all_eight_suppression_shapes() -> None:
    events = _corpus("schwab_2024.csv", "schwab_2025.csv")
    _, dropped, vetoed = grouping.suppress(events)
    assert vetoed == []
    assert sum(len(g.refs) for g in dropped) == 16
    dropped_kinds = {k for g in dropped for k in g.kinds}
    assert dropped_kinds == {
        EventKind.JOURNAL_INTERNAL, EventKind.TRANSFER_OUT, EventKind.CASH_SWEEP,
        EventKind.MARK_TO_MARKET, EventKind.CANCELLED, EventKind.BUY,
        EventKind.REINVEST_ADJ, EventKind.DRIP_BUY,
    }


def test_the_corpus_reproduces_the_overlap_between_two_exports() -> None:
    """Consecutive broker exports overlap; the corpus carries the same wire in both files."""
    events = _corpus("schwab_2024.csv", "schwab_2025.csv")
    [(first, second)] = grouping.overlap_duplicates(events)
    assert first.source_file != second.source_file
    assert first.kind is EventKind.DEPOSIT


def test_the_corpus_reinvest_shares_are_derived_not_read() -> None:
    """The corpus's DRIP triple deliberately prints a rounded quantity that does not satisfy
    ``quantity x price == amount`` — the shape 125 of 227 real reinvest rows have."""
    events = _corpus("schwab_2024.csv")
    [dividend] = grouping.group_events(events).dividends
    printed = Decimal("1.8137")
    assert dividend.reinvest_shares == Decimal("47.50") / Decimal("26.19")
    assert dividend.reinvest_shares != printed


def test_the_corpus_dual_date_row_books_on_its_trade_date() -> None:
    events = _corpus("schwab_2025.csv")
    dual = [e for e in events if e.trade_date != e.posted_date]
    assert len(dual) == 1
    assert dual[0].trade_date == date(2025, 12, 15)
    assert dual[0].posted_date == date(2025, 12, 18)


def test_the_corpus_finds_the_pre_history_position() -> None:
    events = _corpus("schwab_2024.csv", "schwab_2025.csv")
    assert grouping.prehistory_shares(events) == {"PREH": Decimal("80")}


def test_the_corpus_proves_rule_7_is_live() -> None:
    """A rule that is written down but never exercised is a rule nobody knows is broken.
    This file exists only to be refused."""
    with pytest.raises(UnmappedRow) as excinfo:
        _corpus("schwab_unmapped.csv")
    assert excinfo.value.source_file == "schwab_unmapped.csv"
