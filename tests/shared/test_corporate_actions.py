"""W1 — the corporate-action algebra, tested standalone before anything is wired to it.

The two tests that carry the most weight here are the DETECTION-POWER ones. A test that
only asserts the correct form gives the right answer would stay green if someone
"simplified" `qty * to / from` into `qty * (to / from)` — so each rule is paired with a
test that pins what the WRONG form produces. If a future edit makes those two agree, the
suite goes red, which is the whole point.
"""

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from portfolio_dash.shared.corporate_actions import (
    ActionIndex,
    CorporateAction,
    CorporateActionKind,
    apply_ratio,
    is_ratio_term,
    split_factor,
)
from portfolio_dash.shared.ledger_events import EventPriority

D = Decimal
DAY = date(2026, 6, 15)


def _act(
    kind: CorporateActionKind = CorporateActionKind.SPLIT,
    *,
    to: str = "3",
    frm: str = "1",
    day: date = DAY,
    account: str = "schwab",
    from_symbol: str = "AAA",
    to_symbol: str | None = None,
    cost_carry: str | None = None,
) -> CorporateAction:
    return CorporateAction(
        account_id=account, date=day, kind=kind,
        from_symbol=from_symbol,
        to_symbol=to_symbol if to_symbol is not None else from_symbol,
        ratio_to=D(to), ratio_from=D(frm),
        cost_carry=None if cost_carry is None else D(cost_carry),
    )


# --------------------------------------------------------------------- apply_ratio


def test_two_for_seven_of_700_is_exactly_200() -> None:
    """The case the whole two-term design exists for (spec §3.1(ii))."""
    result = apply_ratio(D("700"), _act(to="2", frm="7"))
    assert result == D("200")
    # …and a later sell of 200 must pass validate.py's bare `>` comparison, no epsilon.
    assert not (D("200") > result)


@pytest.mark.parametrize(
    ("qty", "to", "frm", "expected"),
    [
        ("85", "3", "1", "255"),        # TSLA-shaped forward split
        ("200", "1", "20", "10"),       # 1-for-20 reverse split
        ("100", "3", "5", "60"),        # 0.6 exchange ratio, entered as a fraction
        ("1000", "8", "5", "1600"),
        ("0", "3", "1", "0"),           # flat position stays flat
    ],
)
def test_apply_ratio_exact_cases(qty: str, to: str, frm: str, expected: str) -> None:
    assert apply_ratio(D(qty), _act(to=to, frm=frm)) == D(expected)


@pytest.mark.parametrize(("qty", "to", "frm", "exact"), [("3", "1", "3", "1"),
                                                         ("935", "18", "17", "990")])
def test_evaluation_order_is_load_bearing(qty: str, to: str, frm: str, exact: str) -> None:
    """DETECTION POWER for §3.1(ii)(a): pin what the WRONG order produces.

    These two fixtures are measured counter-examples from the 400,000-pair sweep — the
    multiply-first form lands exactly on the integer and the parenthesised form lands one
    ulp below it. Without this test, rewriting `apply_ratio` as `qty * (to / from)` is a
    silent change; with it, the rewrite fails here.

    (The 2-for-7 case above canNOT serve this purpose: measured, both forms give exactly
    200 there — the spec originally mandated that companion test and it was unsatisfiable.)
    """
    action = _act(to=to, frm=frm)
    correct = apply_ratio(D(qty), action)
    parenthesised = D(qty) * (action.ratio_to / action.ratio_from)
    assert correct == D(exact), "multiply-first must hit the integer"
    assert parenthesised != D(exact), "the parenthesised form must NOT — else this test is blind"
    assert parenthesised < D(exact)
    # The consequence that makes it a defect rather than a curiosity: the oversell guard.
    assert not (D(exact) > correct)          # a sell of the full position passes
    assert D(exact) > parenthesised          # …and would trip on the wrong form


# --------------------------------------------------------------------- ratio terms


@pytest.mark.parametrize("term", ["1", "3", "20", "10000", "3.0", "8161"])
def test_is_ratio_term_accepts_positive_integers(term: str) -> None:
    assert is_ratio_term(D(term))


@pytest.mark.parametrize("term", ["0", "-1", "-3", "0.2857", "1.5", "0.0001", "NaN", "Infinity"])
def test_is_ratio_term_rejects_everything_else(term: str) -> None:
    assert not is_ratio_term(D(term))


def test_model_rejects_a_rounded_decimal_ratio() -> None:
    """D14: 'Decimal > 0' admitted 0.2857 — the exact input that recreates the cascade."""
    with pytest.raises(ValidationError, match="positive integer"):
        _act(to="0.2857", frm="1")


def test_the_rounded_ratio_would_have_produced_the_cascade() -> None:
    """DETECTION POWER for D14: pin the number the rejected input yields.

    Without the integer rule, 700 shares of a 2-for-7 exchange become 199.9900 — 0.01
    shares short — and a later sell of 200 trips validate.py's bare `>`, whose acknowledged
    oversell then discards the position's cost basis permanently. Asserting this here means
    the guard cannot be quietly relaxed back to 'Decimal > 0' without the suite noticing.
    """
    bad = D("700") * D("0.2857") / D("1")
    assert bad == D("199.9900")
    assert D("200") > bad, "this is the comparison that trips the 賣超 guard"
    assert not is_ratio_term(D("0.2857"))


def test_model_rejects_negative_and_zero_terms() -> None:
    with pytest.raises(ValidationError, match="positive integer"):
        _act(to="0", frm="1")
    with pytest.raises(ValidationError, match="positive integer"):
        _act(to="3", frm="-1")


# --------------------------------------------------------------------- cost_carry


def test_spinoff_requires_cost_carry_in_range() -> None:
    ok = _act(CorporateActionKind.SPINOFF, to="1", frm="1",
              to_symbol="BBB", cost_carry="0.30")
    assert ok.cost_carry == D("0.30")
    with pytest.raises(ValidationError, match="requires cost_carry"):
        _act(CorporateActionKind.SPINOFF, to_symbol="BBB")
    with pytest.raises(ValidationError, match=r"\[0, 1\]"):
        _act(CorporateActionKind.SPINOFF, to_symbol="BBB", cost_carry="1.5")


def test_cost_carry_is_spinoff_only() -> None:
    with pytest.raises(ValidationError, match="SPINOFF-only"):
        _act(CorporateActionKind.SPLIT, cost_carry="0.30")
    with pytest.raises(ValidationError, match="SPINOFF-only"):
        _act(CorporateActionKind.EXCHANGE, to_symbol="BBB", cost_carry="0.30")


# --------------------------------------------------------------------- ActionIndex


def test_index_orders_by_date_and_groups_both_ways() -> None:
    later = _act(CorporateActionKind.EXCHANGE, to="1", frm="1",
                 day=date(2026, 9, 1), from_symbol="AAA", to_symbol="BBB")
    earlier = _act(day=date(2026, 3, 1))
    idx = ActionIndex.build([later, earlier])
    assert [a.date for a in idx.all] == [date(2026, 3, 1), date(2026, 9, 1)]
    assert idx.by_source("schwab", "AAA") == (earlier, later)
    assert idx.by_dest("schwab", "BBB") == (later,)
    assert idx.by_source("schwab", "ZZZ") == ()
    assert idx.by_dest("other_account", "BBB") == ()


def test_index_deduplicates_a_split_held_in_several_accounts() -> None:
    """One price event, N ledger rows. Multiplying all of them makes a 3-for-1 into 27-for-1."""
    rows = [_act(account=a) for a in ("schwab", "moomoo_my", "tw_broker")]
    idx = ActionIndex.build(rows)
    assert len(idx.splits_on("AAA")) == 1
    assert split_factor(idx, "AAA", after=date(2026, 1, 1), through=date(2026, 12, 31)) == D("3")


def test_index_keeps_distinct_splits_on_the_same_symbol() -> None:
    """Dedup is on (symbol, date, ratio) — two genuinely different splits must both count."""
    idx = ActionIndex.build([
        _act(to="3", frm="1", day=date(2026, 3, 1)),
        _act(to="2", frm="1", day=date(2026, 9, 1)),
    ])
    assert len(idx.splits_on("AAA")) == 2
    assert split_factor(idx, "AAA", after=date(2026, 1, 1), through=date(2026, 12, 31)) == D("6")


# --------------------------------------------------------------------- split_factor


def test_split_factor_window_is_exclusive_left_inclusive_right() -> None:
    idx = ActionIndex.build([_act(day=DAY)])
    assert split_factor(idx, "AAA", after=DAY, through=date(2026, 12, 31)) == D("1")
    assert split_factor(idx, "AAA", after=date(2026, 1, 1), through=DAY) == D("3")
    assert split_factor(idx, "AAA", after=date(2026, 1, 1),
                        through=DAY.replace(day=14)) == D("1")


def test_split_factor_empty_window_is_exactly_one() -> None:
    """The identity, not 1.000…0 — callers compare it against a stored basis."""
    idx = ActionIndex.build([])
    factor = split_factor(idx, "AAA", after=date(2026, 1, 1), through=date(2026, 12, 31))
    assert factor == D("1")
    assert str(factor) == "1"


def test_split_factor_ignores_exchange_and_spinoff() -> None:
    """D22: scope is SPLIT-only, and it must EXCLUDE as reliably as it includes.

    An EXCHANGE adds to its destination rather than re-denominating it, so applying a
    factor here would corrupt the stored price history of a symbol held before the merger.
    """
    idx = ActionIndex.build([
        _act(CorporateActionKind.EXCHANGE, to="1", frm="20", from_symbol="AAA", to_symbol="BBB"),
        _act(CorporateActionKind.SPINOFF, to="1", frm="10", from_symbol="AAA",
             to_symbol="CCC", cost_carry="0.3"),
    ])
    assert idx.splits_on("AAA") == ()
    for sym in ("AAA", "BBB", "CCC"):
        assert split_factor(idx, sym, after=date(2020, 1, 1), through=date(2030, 1, 1)) == D("1")


def test_split_factor_reverse_split() -> None:
    idx = ActionIndex.build([_act(to="1", frm="20")])
    assert split_factor(idx, "AAA", after=date(2026, 1, 1), through=date(2026, 12, 31)) == D("0.05")


def test_split_factor_is_symbol_scoped() -> None:
    idx = ActionIndex.build([_act(from_symbol="AAA")])
    assert split_factor(idx, "BBB", after=date(2026, 1, 1), through=date(2026, 12, 31)) == D("1")


# --------------------------------------------------------------------- EventPriority


def test_event_priority_places_actions_before_the_day_s_trades() -> None:
    """An action re-denominates a position before that day's trades, which are quoted in
    post-action terms; opening inventory must already be seeded."""
    assert EventPriority.OPENING < EventPriority.CORPORATE_ACTION < EventPriority.BUY
    assert EventPriority.BUY < EventPriority.SELL < EventPriority.DIVIDEND


def test_event_priority_is_spaced_so_the_next_insert_moves_nothing() -> None:
    values = [p.value for p in EventPriority]
    assert values == sorted(values)
    # Deliberately offset by one, so `strict` must stay off here.
    assert all(b - a >= 10 for a, b in zip(values, values[1:], strict=False))


def test_event_priority_sorts_as_an_int() -> None:
    """It is used as a sort key alongside dates in build_book — IntEnum, not Enum."""
    assert sorted([EventPriority.DIVIDEND, EventPriority.OPENING, EventPriority.CORPORATE_ACTION]) \
        == [EventPriority.OPENING, EventPriority.CORPORATE_ACTION, EventPriority.DIVIDEND]
    assert int(EventPriority.CORPORATE_ACTION) == 10
