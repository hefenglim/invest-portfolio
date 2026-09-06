"""D38 invariant 1 — a symbol with no corporate action never enters the new code.

The owner's ruling, and the reason this file exists rather than a value-equality assertion:

    Prefer a **structural short-circuit** over an equivalent computation. Where a symbol has
    no corporate action, take the **pre-existing code path** — do not run the new walk and
    rely on it producing the same answer. *Code that does not execute cannot drift; code
    that computes an equal answer can.*

W4 makes ``shares_through`` action-aware for **every symbol in the system**, which is the
largest blast radius in the remaining plan. So the containment is proved three ways, in
increasing order of strength:

* **value** — each wrapper equals the untouched :func:`_shares_until` with the argument it
  has always received, over a few hundred randomly generated action-free ledgers;
* **structural** — :class:`_Walk` is made impossible to construct, and every wrapper and
  every validation door still works. This is the assertion the ruling actually asks for: not
  "the answers match" but "the new code did not run";
* **replay-level** — the spec's literal wording: a symbol's ``Holding`` in ``build_book(L)``
  equals its ``Holding`` in ``build_book(L with every action removed)``.

One honest exception is recorded in :func:`test_the_one_deliberate_change_to_the_naive_path`.
"""

import random
import re
import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion import holdings as holdings_mod
from portfolio_dash.data_ingestion.holdings import (
    _shares_until,
    current_shares,
    shares_on,
    shares_through,
)
from portfolio_dash.data_ingestion.store import (
    insert_dividend,
    insert_transaction,
    load_ledger_bundle,
    upsert_instrument,
    upsert_opening,
)
from portfolio_dash.data_ingestion.validate import (
    CorporateActionInput,
    TxnInput,
    validate_corporate_action,
    validate_transaction,
)
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.results import Book, Holding
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

D = Decimal
START = date(2026, 1, 1)
SYMS = ("AAA", "BBB", "CCC")
ACCTS = ("schwab", "moomoo_my")

# Every production call site of the three wrappers. Pinned as a FACT, not a claim: a tenth
# call site added without thinking about containment turns this list stale, and a stale list
# is how "nine call sites" became folklore in the first place.
EXPECTED_CALL_SITES = {
    ("portfolio_dash/api/dividend_inbox.py", "shares_on"),
    ("portfolio_dash/api/routers/input_center.py", "current_shares"),
    # W5 (spec §6.3): the symbol drawer's `corporate_delta` — `shares_action_aware −
    # shares_naive`, this being the action-aware half. Containment holds for it the same
    # structural way as for the other eight: an action-free symbol short-circuits inside
    # `_shares_at`, the walk does not run, and the delta is an exact `Decimal("0")`, so the
    # reconciliation footer of every un-actioned symbol is byte-identical to pre-feature.
    ("portfolio_dash/api/routers/symbol.py", "current_shares"),
    ("portfolio_dash/api/routers/instruments.py", "current_shares"),
    ("portfolio_dash/api/routers/strategy.py", "current_shares"),
    ("portfolio_dash/api/signals_service.py", "current_shares"),
    # W3 (AI-D16): the AV-leg runner's held set — the same net-shares-positive predicate
    # as strategy.py's `_held_set`, via the same action-aware wrapper. Containment holds
    # the same structural way: an action-free symbol short-circuits inside `_shares_at`,
    # and a held check against it is byte-identical to the pre-feature answer.
    ("portfolio_dash/api/fundamentals_service.py", "current_shares"),
    # M10-02 (2026-09-06): the quote jobs' partial threshold — "did a HELD instrument
    # fail?" — answered by `actions.py::held_symbols` and injected into `scheduler/jobs.py`,
    # which may not import `data_ingestion` itself. It is the SAME net-shares-positive
    # predicate as `strategy.py::_held_set` and `fundamentals_service.py::_held_refs`, via
    # the same action-aware wrapper, so containment holds the same structural way: an
    # action-free symbol short-circuits inside `_shares_at`, the walk never runs, and the
    # held answer is byte-identical to the pre-feature one.
    ("portfolio_dash/api/routers/actions.py", "current_shares"),
    ("portfolio_dash/data_ingestion/validate.py", "current_shares"),
    ("portfolio_dash/data_ingestion/validate.py", "shares_through"),
    # E1a (spec §5, 2026-08-11): the ONE accessor that deliberately skips the structural
    # short-circuit, so unlike the other nine it always walks. Containment is unaffected:
    # `validate_corporate_action` runs only when an action is being ENTERED, so there is no
    # action-free ledger on which this can fire, and the function did not exist before this
    # feature — there is no earlier behaviour to be byte-identical to. See
    # `test_the_ACTION_door_legitimately_enters_the_walk` for the full argument.
    ("portfolio_dash/data_ingestion/validate.py", "shares_before_action_on"),
    # W7 (spec §6.7): the corporate-action form builds E13's COMPLETE batch itself, so it
    # has to answer the same question E13 asks — which accounts hold this symbol on the
    # action date — and it must answer it the SAME way, or the form would submit a batch
    # its own validator rejects. Two uses, one wrapper: `_holding_accounts` (the batch) and
    # `_unblocked_sells` (does the action clear a currently-failing sell?). Containment is
    # unchanged and structural: an action-free symbol short-circuits inside `_shares_at`,
    # the walk never runs, and both answers are byte-identical to pre-feature.
    ("portfolio_dash/api/routers/ledgers.py", "shares_through"),
}


def _blank_db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    c.executemany(
        "INSERT INTO accounts (account_id, name, broker, settlement_ccy, funding_ccy, "
        "fee_rule_set, dividend_model) VALUES (?,?,?,?,?,?,?)",
        [("schwab", "Schwab", "Schwab", "USD", "TWD", "schwab_us", "drip_us"),
         ("moomoo_my", "Moomoo", "Moomoo MY", "USD", "MYR", "moomoo_us", "drip_us")],
    )
    for sym in SYMS:
        upsert_instrument(c, Instrument(symbol=sym, market=Market.US,
                                        quote_ccy=Currency.USD, sector="Tech", name=sym))
    return c


def _random_action_free_ledger(rng: random.Random) -> sqlite3.Connection:
    """An arbitrary ledger with NO ``corporate_actions`` row — every other ledger populated."""
    c = _blank_db()
    for acct in ACCTS:
        for sym in SYMS:
            if rng.random() < 0.4:
                upsert_opening(c, account_id=acct, symbol=sym,
                               shares=D(str(rng.randint(1, 500))),
                               original_cost_total=D(str(rng.randint(1, 50_000))),
                               build_date=START + timedelta(days=rng.randint(0, 30)),
                               commit=False)
    for _ in range(rng.randint(0, 12)):
        insert_transaction(
            c, account_id=rng.choice(ACCTS), symbol=rng.choice(SYMS),
            side=rng.choice([Side.BUY, Side.SELL]), quantity=D(str(rng.randint(1, 100))),
            price=D(str(rng.randint(1, 900))), fees=D("1"), tax=D("0"),
            trade_date=START + timedelta(days=rng.randint(0, 300)), commit=False,
        )
    for _ in range(rng.randint(0, 6)):
        kind = rng.choice(["CASH", "NET", "DRIP", "STOCK"])
        insert_dividend(
            c, account_id=rng.choice(ACCTS), symbol=rng.choice(SYMS),
            div_date=START + timedelta(days=rng.randint(0, 300)), div_type=kind,
            gross=D("10"), withholding=D("0"), net=D("10"),
            reinvest_shares=D(str(rng.randint(1, 9))), reinvest_price=D("1.5"),
            commit=False,
        )
    c.commit()
    return c


@pytest.fixture
def blank() -> Iterator[sqlite3.Connection]:
    c = _blank_db()
    yield c
    c.close()


# ------------------------------------------------------------------ value equality


def test_every_wrapper_equals_the_untouched_naive_path() -> None:
    """Over 300 random action-free ledgers, all three wrappers return ``_shares_until``."""
    rng = random.Random(20260810)
    probe_dates = [START + timedelta(days=d) for d in (0, 1, 45, 150, 301)]
    for _ in range(300):
        c = _random_action_free_ledger(rng)
        try:
            for acct in ACCTS:
                for sym in SYMS:
                    assert current_shares(c, acct, sym) == _shares_until(c, acct, sym, None)
                    for day in probe_dates:
                        assert shares_through(c, acct, sym, on=day) == _shares_until(
                            c, acct, sym, day + timedelta(days=1))
                        assert shares_on(c, acct, sym, before=day) == _shares_until(
                            c, acct, sym, day)
        finally:
            c.close()


# -------------------------------------------------------------- structural proof


def test_the_walker_is_never_constructed_without_a_corporate_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ruling's actual assertion: the new code does not RUN, not "it agrees".

    ``_Walk`` is replaced with something that explodes on construction, so any path that
    reaches the action-aware branch fails loudly instead of quietly producing a matching
    number. A value test cannot make this statement — that is the whole point of preferring
    a structural short-circuit.
    """

    def landmine(*a: object, **kw: object) -> None:
        raise AssertionError("the action-aware walk ran for an action-free ledger")

    monkeypatch.setattr(holdings_mod, "_Walk", landmine)
    rng = random.Random(4242)
    for _ in range(60):
        c = _random_action_free_ledger(rng)
        try:
            for acct in ACCTS:
                for sym in SYMS:
                    current_shares(c, acct, sym)
                    shares_through(c, acct, sym, on=date(2026, 7, 1))
                    shares_on(c, acct, sym, before=date(2026, 7, 1))
        finally:
            c.close()


def test_the_TRADE_door_stays_out_of_the_walk(
    blank: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same landmine, driven through the door that runs on EVERY trade.

    This is the load-bearing half of D38 invariant 1 at the validation layer:
    `validate_transaction` fires on every trade on every ledger, including ledgers that
    will never hold a corporate action, so the new machinery must not execute there at all.
    Not "must agree" — must **not run**.
    """
    insert_transaction(blank, account_id="schwab", symbol="AAA", side=Side.BUY,
                       quantity=D("100"), price=D("10"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 1, 5))

    def landmine(*a: object, **kw: object) -> None:
        raise AssertionError("the action-aware walk ran for an action-free ledger")

    monkeypatch.setattr(holdings_mod, "_Walk", landmine)
    assert validate_transaction(blank, TxnInput(
        account_id="schwab", symbol="AAA", side=Side.SELL, quantity=D("40"),
        price=D("12"), trade_date=date(2026, 2, 1))) == []


def test_the_ACTION_door_legitimately_enters_the_walk(blank: sqlite3.Connection) -> None:
    """`validate_corporate_action` DOES walk, and that is correct (changed 2026-08-11).

    This assertion used to sit beside the one above, and splitting them is the point rather
    than an accommodation. The two doors are not alike:

    * `validate_transaction` runs on ledgers that may never hold an action — containment
      there is about a feature not reaching code it has no business in.
    * `validate_corporate_action` runs **only when an action is being entered**. An
      "action-free ledger" here means *the first action ever*, and the walk running at that
      moment is the feature doing its job, not leaking.

    It became necessary rather than merely permissible when E1a moved onto the action's own
    cut, `(date, CORPORATE_ACTION)` — the naive path **cannot express that cut**:
    `_shares_until` applies one `<` bound to all three ledgers, which is F-18's defect, and
    it is why `shares_before_action_on` skips the short-circuit deliberately.

    D38 invariant 1 is untouched by this. It says a symbol with no corporate action behaves
    exactly as it did **before this feature existed** — and `validate_corporate_action` did
    not exist before this feature, so there is no earlier behaviour to preserve. What must
    stay true is that the *answer* is right, which the tests in
    `test_action_validated_at_its_own_date.py` pin from both boundary directions.
    """
    insert_transaction(blank, account_id="schwab", symbol="AAA", side=Side.BUY,
                       quantity=D("100"), price=D("10"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 1, 5))
    kinds = {i.kind for i in validate_corporate_action(blank, CorporateActionInput(
        account_id="schwab", date=date(2026, 3, 1), kind="SPLIT", from_symbol="AAA",
        to_symbol="AAA", ratio_to=D("3"), ratio_from=D("1")))}
    assert kinds == set()


def test_the_call_site_census_is_still_accurate() -> None:
    """Pin WHICH modules reach the three wrappers, so a tenth call site is a visible event."""
    root = Path(__file__).resolve().parents[2] / "portfolio_dash"
    # `shares_before_action_on` FIRST: alternation is ordered, and it must not be shadowed.
    # It was invisible to this census when it landed (no `\bshares_on` substring inside it),
    # which would have made the tenth accessor the one nobody had to argue for.
    pattern = re.compile(
        r"\b(shares_before_action_on|current_shares|shares_through|shares_on)\s*\(")
    found: set[tuple[str, str]] = set()
    for path in root.rglob("*.py"):
        if path.name == "holdings.py":
            continue                          # the definitions themselves
        for name in pattern.findall(path.read_text(encoding="utf-8")):
            found.add((path.relative_to(root.parent).as_posix(), name))
    assert found == EXPECTED_CALL_SITES, (
        "the set of share-count call sites changed; D38 invariant 1 covers every one of "
        f"them — added {found - EXPECTED_CALL_SITES}, removed {EXPECTED_CALL_SITES - found}"
    )


# ----------------------------------------------------------------- replay level


def test_an_action_free_symbol_is_identical_with_and_without_the_action_ledger() -> None:
    """§8.1 invariant 1, literally: BBB's ``Holding`` is untouched by AAA's corporate action."""
    import dataclasses

    c = _blank_db()
    try:
        for sym in ("AAA", "BBB"):
            insert_transaction(c, account_id="schwab", symbol=sym, side=Side.BUY,
                               quantity=D("100"), price=D("50"), fees=D("1"), tax=D("0"),
                               trade_date=date(2026, 1, 10))
        insert_dividend(c, account_id="schwab", symbol="BBB", div_date=date(2026, 4, 1),
                        div_type="CASH", gross=D("40"), withholding=D("0"), net=D("40"))
        from portfolio_dash.data_ingestion.store import insert_corporate_action
        from portfolio_dash.shared.corporate_actions import CorporateActionKind
        insert_corporate_action(c, account_id="schwab", action_date=date(2026, 5, 1),
                                kind=CorporateActionKind.SPLIT, from_symbol="AAA",
                                to_symbol="AAA", ratio_to=D("7"), ratio_from=D("1"))

        bundle = load_ledger_bundle(c)
        with_actions = build_book(bundle)
        without = build_book(dataclasses.replace(bundle, actions=[]))
        def pick(bk: Book, s: str) -> Holding:
            return next(h for h in bk.holdings if h.symbol == s)

        assert pick(with_actions, "BBB") == pick(without, "BBB")
        assert pick(with_actions, "AAA") != pick(without, "AAA")   # the fixture is live
    finally:
        c.close()


def test_the_one_deliberate_change_to_the_naive_path(blank: sqlite3.Connection) -> None:
    """F-12: the ONE way an action-free symbol's naive count changed in W4, stated openly.

    ``_shares_until``'s SQL said ``type != 'CASH'``, which admits ``NET`` — but ``build_book``
    books NET as CASH and adds no shares (``CASH_DIVIDEND_TYPES``), so a NET row carrying
    ``reinvest_shares`` made the two paths disagree on a symbol with no corporate action
    whatsoever, and §6.3's ``corporate_delta`` would have blamed the gap on this feature.

    This is a deliberate exception to "byte-identical to pre-W4", and it moves TOWARD the
    invariant rather than away from it: after the fix the naive path agrees with the replay,
    which is what the containment is for. Recorded here rather than buried in a diff.
    """
    insert_transaction(blank, account_id="schwab", symbol="AAA", side=Side.BUY,
                       quantity=D("100"), price=D("10"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 1, 5))
    insert_dividend(blank, account_id="schwab", symbol="AAA", div_date=date(2026, 3, 1),
                    div_type="NET", gross=D("30"), withholding=D("0"), net=D("30"),
                    reinvest_shares=D("7"), reinvest_price=D("4"))
    replayed = next(h for h in build_book(load_ledger_bundle(blank)).holdings
                    if h.symbol == "AAA")
    assert replayed.shares == D("100")
    assert current_shares(blank, "schwab", "AAA") == D("100")   # 107 before the fix

    # DRIP and STOCK still add their shares — the fix narrowed the set, it did not empty it.
    insert_dividend(blank, account_id="schwab", symbol="AAA", div_date=date(2026, 4, 1),
                    div_type="DRIP", gross=D("30"), withholding=D("9"), net=D("21"),
                    reinvest_shares=D("3"), reinvest_price=D("7"))
    insert_dividend(blank, account_id="schwab", symbol="AAA", div_date=date(2026, 4, 2),
                    div_type="STOCK", gross=D("0"), withholding=D("0"), net=D("0"),
                    reinvest_shares=D("5"), reinvest_price=D("0"))
    assert current_shares(blank, "schwab", "AAA") == D("108")
