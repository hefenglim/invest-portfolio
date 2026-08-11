"""The price-basis reconcile (spec §5.1(c) + §7.1b, D30 · W6b).

W6a made the write seam store three related columns — ``close_raw`` (the provider value
exactly as delivered), ``split_basis`` (the factor applied) and ``close`` (their capped
product). W6b makes the corporate-action ledger able to *move* that factor: entering,
editing or deleting a SPLIT restates every affected close as ``close := close_raw ×
target_new``, recomputed from the stored raw value — never from the current close, and
never by dividing the old basis back out. D30 rejected that reconstruction with measured
numbers, and §7.1b's four clauses are the assertions that keep it rejected.

**Every load-bearing assertion is on the stored TEXT.** ``Decimal("1.5") ==
Decimal("1.50")`` is ``True``, so value equality cannot see a representation change — and a
representation change is exactly what D38 invariant 3 exists to forbid, because ``prices``
is the only place this feature writes outside the ledgers and therefore the only mutation
重算 does not cover.

The two windows, stated once because conflating them is the defect an implementer reaches
for (they look like the same product applied twice, and they are not):

* the **write** window is ``(as_of, fetched_at]`` — "which splits had the provider already
  folded into this delivered number?" — and the seam multiplies them back OUT, so the
  stored close is as-traded on its own date;
* the **read** window is ``(pd, d]`` — "which splits happened between this price's date and
  the day I am valuing?" — and §5.1(d) DIVIDES by them.

``test_a_row_fetched_before_and_after_a_split_value_the_same_day_alike`` is the proof they
compose rather than compound.
"""

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal

import pytest

from portfolio_dash.api.instrument_service import reconcile_price_basis
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.store import (
    delete_corporate_action,
    insert_corporate_action,
    update_corporate_action,
)
from portfolio_dash.portfolio.price_basis import price_in
from portfolio_dash.pricing.reconcile import reconcile_prices
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import SplitFactorFn, upsert_prices
from portfolio_dash.shared.corporate_actions import (
    ActionIndex,
    CorporateAction,
    CorporateActionKind,
    split_factor,
)
from portfolio_dash.shared.enums import Market

# Fetched AFTER both of the splits used below, so the provider has already re-stated its
# history and the write/reconcile factor is non-trivial. A row fetched BEFORE a split has
# an empty window and is correctly left alone — see the "no action" and "either order"
# tests for both halves of that.
_FETCHED = datetime(2026, 6, 25, 12, 0, 0)
_DAY = date(2026, 6, 5)

# A real yfinance float tail: what a float-sourced provider actually delivers, and the
# value D30's order-independence argument was measured on.
_FLOAT_TAIL = "0.14166666865348816"

# Two SPLITs whose ORDER of entry changes the answer under the rejected single-column
# design: rebuilding in place gives 0.1652 one way and 0.1653 the other (measured with the
# module's own helpers), and neither equals the one-shot 0.1653. Rebuilding from
# ``close_raw`` caps once, on one product, so both orders land on 0.1653.
_REVERSE_1_FOR_3 = (Decimal("1"), Decimal("3"), date(2026, 6, 10))
_SPLIT_7_FOR_2 = (Decimal("7"), Decimal("2"), date(2026, 6, 20))


def _action(
    terms: tuple[Decimal, Decimal, date],
    *,
    symbol: str = "AAPL",
    kind: CorporateActionKind = CorporateActionKind.SPLIT,
    account_id: str = "schwab",
) -> CorporateAction:
    to_, from_, when = terms
    return CorporateAction(account_id=account_id, date=when, kind=kind,
                           from_symbol=symbol, to_symbol=symbol,
                           ratio_to=to_, ratio_from=from_)


def _factor_of(*actions: CorporateAction) -> SplitFactorFn:
    """The REAL injected factor: one :class:`ActionIndex`, the real :func:`split_factor`.

    Mirrors ``scheduler.jobs.split_factor_fn`` minus its SELECT, so these tests exercise
    the production algebra — the dedup key, the reduced fraction, the half-open window —
    rather than a stand-in that could agree with a wrong implementation.
    """
    index = ActionIndex.build(actions)

    def factor_of(symbol: str, *, after: date, through: date) -> Decimal:
        return split_factor(index, symbol, after=after, through=through)

    return factor_of


def _row(close: str, *, sym: str = "AAPL", d: date = _DAY) -> PriceRow:
    return PriceRow(instrument=sym, market=Market.US, as_of=d, close=Decimal(close),
                    source="fake")


def _snapshot(conn: sqlite3.Connection, sym: str = "AAPL") -> list[tuple[str, ...]]:
    """Every stored price row of *sym* as raw TEXT — the byte-level comparison unit."""
    return [
        (r["as_of_date"], r["close"], r["close_raw"], r["split_basis"])
        for r in conn.execute(
            "SELECT as_of_date, close, close_raw, split_basis FROM prices "
            "WHERE instrument=? ORDER BY as_of_date", (sym,),
        )
    ]


def _close(conn: sqlite3.Connection, sym: str = "AAPL", d: date = _DAY) -> str:
    row = conn.execute("SELECT close FROM prices WHERE instrument=? AND as_of_date=?",
                       (sym, d.isoformat())).fetchone()
    assert row is not None
    return str(row["close"])


@pytest.fixture
def ledger_conn() -> Iterator[sqlite3.Connection]:
    """A connection carrying BOTH schemas, for the application-level entry point.

    ``reconcile_price_basis`` binds the real ``split_factor_fn(conn)``, which reads the
    ``corporate_actions`` table — so the end-to-end test needs the ledger tables that
    ``tests/pricing/conftest.py``'s pricing-only fixture deliberately does not create.
    """
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    create_pricing_tables(c)
    yield c
    c.close()


# --- §7.1b clause 1 — run reconcile twice -------------------------------------------


def test_running_the_reconcile_twice_is_byte_identical(conn: sqlite3.Connection) -> None:
    """Idempotent BY CONSTRUCTION, and asserted on the stored TEXT (§7.1b, D30).

    The superseded design keyed off ``fetched_at >= action.date``, which stays true
    forever, so a second pass multiplied the price again. Rebuilding ``close := raw ×
    target`` from an unchanged input cannot: the second pass recomputes the same product
    from the same raw value.
    """
    upsert_prices(conn, [_row(_FLOAT_TAIL)], fetched_at=_FETCHED)
    factor_of = _factor_of(_action(_SPLIT_7_FOR_2))

    assert reconcile_prices(conn, ["AAPL"], factor_of=factor_of) == 1
    after_first = _snapshot(conn)
    assert after_first == [(_DAY.isoformat(), "0.4958", _FLOAT_TAIL, "3.5")]

    # The second pass must write NOTHING and change NOTHING.
    assert reconcile_prices(conn, ["AAPL"], factor_of=factor_of) == 0
    assert _snapshot(conn) == after_first


# --- §7.1b clause 2 — edit the ratio -------------------------------------------------


def test_editing_the_ratio_lands_on_the_new_value_not_the_compounded_one(
    conn: sqlite3.Connection,
) -> None:
    """A 2-for-1 corrected to a 20-for-1 stores ``raw × 20``, never ``raw × 2 × 20``.

    E16 guarantees an edit re-computes history, so this path is scheduled, not
    hypothetical. Detection power is in the third assertion: the compounded figure is
    stated, so an implementation that rescales the CURRENT close instead of rebuilding
    from ``close_raw`` fails here rather than shipping a 40× price.
    """
    upsert_prices(conn, [_row("5")], fetched_at=_FETCHED)
    reconcile_prices(conn, ["AAPL"], factor_of=_factor_of(
        _action((Decimal("2"), Decimal("1"), date(2026, 6, 10)))))
    assert _close(conn) == "10"

    reconcile_prices(conn, ["AAPL"], factor_of=_factor_of(
        _action((Decimal("20"), Decimal("1"), date(2026, 6, 10)))))
    assert _close(conn) == "100"      # 5 x 20
    assert _close(conn) != "200"      # 5 x 2 x 20 — the rescale-in-place answer


# --- §7.1b clause 3 / D38 invariant 3 — delete restores exactly -----------------------


def test_deleting_the_split_restores_the_exact_pre_action_bytes(
    conn: sqlite3.Connection,
) -> None:
    """``prices`` is the only non-replayable mutation in this feature — so it must be
    provably reversible (D38 invariant 3), byte-for-byte and including ``split_basis``.

    Asserted on the full row TEXT rather than on the close alone: a reconcile that
    restored the number but left the basis at ``"3.5"`` would leave the next pass
    rebuilding from a stale marker, and a value comparison could not see it.
    """
    rows = [_row(v, d=date(2026, 6, i + 1))
            for i, v in enumerate(["1.5", "1.50", "600", "0.005", _FLOAT_TAIL])]
    upsert_prices(conn, rows, fetched_at=_FETCHED)
    before = _snapshot(conn)
    assert before == [
        ("2026-06-01", "1.5", "1.5", "1"), ("2026-06-02", "1.50", "1.50", "1"),
        ("2026-06-03", "600", "600", "1"), ("2026-06-04", "0.005", "0.005", "1"),
        ("2026-06-05", "0.1417", _FLOAT_TAIL, "1"),
    ]

    reconcile_prices(conn, ["AAPL"], factor_of=_factor_of(_action(_SPLIT_7_FOR_2)))
    assert _snapshot(conn) != before, "the fixture must actually move, or nothing is proved"

    reconcile_prices(conn, ["AAPL"], factor_of=_factor_of())  # the SPLIT is gone
    assert _snapshot(conn) == before


# --- §7.1b clause 4 — either order, at the STORED VALUE ------------------------------


def test_two_splits_in_either_order_store_the_same_bytes(
    conn: sqlite3.Connection,
) -> None:
    """The clause D30 was ruled on, and it is false for the single-column design.

    Rescaling in place re-applies ``_cap_dp`` on every pass and the two orders traverse
    different intermediates: measured on this exact fixture, 0.1652 one way and 0.1653 the
    other, and **neither** equals the one-shot value. Rebuilding from ``close_raw`` caps
    once, on one product, so the stored value converges with the target.

    The reconcile is run after EACH insertion, because that is what the wiring does — a
    test that only reconciled at the end would converge trivially and prove nothing.
    """
    first, second = _action(_REVERSE_1_FOR_3), _action(_SPLIT_7_FOR_2)
    stored: list[list[tuple[str, ...]]] = []
    for entry_order in ((first, second), (second, first)):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        create_pricing_tables(c)
        upsert_prices(c, [_row(_FLOAT_TAIL)], fetched_at=_FETCHED)
        entered: list[CorporateAction] = []
        for action in entry_order:
            entered.append(action)
            reconcile_prices(c, ["AAPL"], factor_of=_factor_of(*entered))
        stored.append(_snapshot(c))
        c.close()

    assert stored[0] == stored[1]
    # ...and on the ONE-SHOT value, not merely on each other: 7/6 x the raw tail.
    assert stored[0] == [(_DAY.isoformat(), "0.1653", _FLOAT_TAIL,
                          "1.166666666666666666666666667")]


# --- §7.1b clause 5 / D38 invariant 1 — containment ----------------------------------


def test_a_symbol_with_no_corporate_action_is_byte_identical(
    conn: sqlite3.Connection,
) -> None:
    """Containment (D38 invariant 1): a SPLIT on AAPL cannot move one byte of MSFT.

    Mixed trailing zeros on purpose — each is a DIFFERENT canonical string that a
    value-equality assertion would happily accept as unchanged.
    """
    mixed = ["1.5", "1.50", "305.3650", "0.005", "600", "0.4250", _FLOAT_TAIL]
    for sym in ("AAPL", "MSFT"):
        upsert_prices(conn, [_row(v, sym=sym, d=date(2026, 6, i + 1))
                             for i, v in enumerate(mixed)], fetched_at=_FETCHED)
    untouched = _snapshot(conn, "MSFT")

    # Reconcile BOTH symbols, exactly as a caller that does not know which one moved would.
    reconcile_prices(conn, ["AAPL", "MSFT"], factor_of=_factor_of(_action(_SPLIT_7_FOR_2)))

    assert _snapshot(conn, "MSFT") == untouched
    assert {r[3] for r in _snapshot(conn, "MSFT")} == {"1"}
    assert _snapshot(conn, "AAPL") != untouched, "the actioned symbol must have moved"


# --- §7.1 / trap #16 — SPLIT-only scope, proved by exclusion -------------------------


def test_an_exchange_leaves_the_destinations_prices_byte_identical(
    conn: sqlite3.Connection,
) -> None:
    """An EXCHANGE with ``ratio_to != ratio_from`` into a symbol you ALREADY held.

    This is the test that stops a future reader "fixing" §5.1's scope to include EXCHANGE
    when they meet the 95% cliff — an EXCHANGE **adds to** its destination (§4.2's
    ``Q.shares += carried``), it does not re-denominate what was already there, so a factor
    here would corrupt the destination's entire price history. The cliff's real fix is D19,
    at the import seam (D22, trap #16).
    """
    upsert_prices(conn, [_row(v, sym="BBB", d=date(2026, 6, i + 1))
                         for i, v in enumerate(["10", "10.50", "0.4250"])],
                  fetched_at=_FETCHED)
    before = _snapshot(conn, "BBB")

    exchange = CorporateAction(
        account_id="schwab", date=date(2026, 6, 10), kind=CorporateActionKind.EXCHANGE,
        from_symbol="AAA", to_symbol="BBB", ratio_to=Decimal("2"), ratio_from=Decimal("7"),
    )
    assert reconcile_prices(conn, ["AAA", "BBB"], factor_of=_factor_of(exchange)) == 0
    assert _snapshot(conn, "BBB") == before


# --- the write/reconcile agreement + the two windows ---------------------------------


def test_the_reconcile_lands_on_the_bytes_a_refetch_would_write(
    conn: sqlite3.Connection,
) -> None:
    """Reconciling and re-fetching are the SAME restatement (§5.1(c)) — so they must
    produce the same bytes, or "re-fetch is safe" is false again.

    Both go through :func:`~portfolio_dash.pricing.store.express_close`; this asserts the
    property that shared owner exists to guarantee, from the outside.
    """
    factor_of = _factor_of(_action(_SPLIT_7_FOR_2))
    upsert_prices(conn, [_row(_FLOAT_TAIL)], fetched_at=_FETCHED)
    reconcile_prices(conn, ["AAPL"], factor_of=factor_of)
    reconciled = _snapshot(conn)

    other = sqlite3.connect(":memory:")
    other.row_factory = sqlite3.Row
    create_pricing_tables(other)
    upsert_prices(other, [_row(_FLOAT_TAIL)], fetched_at=_FETCHED, factor_of=factor_of)
    assert _snapshot(other) == reconciled
    other.close()


def test_the_reconcile_caps_last_on_the_product(conn: sqlite3.Connection) -> None:
    """F-20 again, on the reconcile path: ``_cap_dp(raw × target, 4)``.

    ``_cap_dp(raw, 4) × target`` amplifies the cap's own error by the factor — 2.8340
    instead of 2.8333 at ×20, and the Bursa sub-RM1 tick 0.4251 instead of 0.4250 at ×3.
    The reconcile reads a STORED value, which makes capping first look even more natural
    here than at the write seam, so the property is pinned on both paths.
    """
    upsert_prices(conn, [_row(_FLOAT_TAIL), _row(_FLOAT_TAIL, d=date(2026, 6, 6))],
                  fetched_at=_FETCHED)
    reconcile_prices(conn, ["AAPL"], factor_of=_factor_of(
        _action((Decimal("20"), Decimal("1"), date(2026, 6, 10)))))
    assert _close(conn) == "2.8333"                        # cap-first stores 2.8340
    reconcile_prices(conn, ["AAPL"], factor_of=_factor_of(
        _action((Decimal("3"), Decimal("1"), date(2026, 6, 10)))))
    assert _close(conn) == "0.4250"                        # cap-first stores 0.4251


def test_an_identity_target_does_not_repaint_the_stored_text(
    conn: sqlite3.Connection,
) -> None:
    """The W6a trap, on the reconcile path: a factor equal to one must not add a decimal.

    ``Decimal`` multiplication sums the operands' EXPONENTS, so computing ``1.5 × 1.0``
    stores ``1.50`` — value-preserving, TEXT-changing, and ``_cap_dp`` does not catch it.
    The reconcile runs over EVERY row of a symbol, so getting this wrong repaints a whole
    price history in one pass rather than one row per refresh.

    Two identities, because they fail differently:

    * a 3-for-3 SPLIT — legal integer terms that reduce — which ``split_factor`` returns as
      an exact ``Decimal(1)``. Nothing may be written at all;
    * an injected ``Decimal("1.0")``. ``split_factor`` cannot produce this today, but the
      factor is an INJECTED callable and this is the one shape that slips past a value
      comparison. Without the structural short-circuit in ``express_close`` this branch
      rewrites every row's TEXT (measured: ``1.5 → 1.50``, ``600 → 600.0``,
      ``0.005 → 0.0050``).
    """
    mixed = ["1.5", "1.50", "600", "0.005", "0.4250"]
    upsert_prices(conn, [_row(v, d=date(2026, 6, i + 1)) for i, v in enumerate(mixed)],
                  fetched_at=_FETCHED)
    before = _snapshot(conn)
    assert reconcile_prices(conn, ["AAPL"], factor_of=_factor_of(
        _action((Decimal("3"), Decimal("3"), date(2026, 6, 10))))) == 0
    assert _snapshot(conn) == before

    def scaled_identity(symbol: str, *, after: date, through: date) -> Decimal:
        return Decimal("1.0")

    assert reconcile_prices(conn, ["AAPL"], factor_of=scaled_identity) == 0
    assert _snapshot(conn) == before


def test_a_row_fetched_before_and_after_a_split_value_the_same_day_alike(
    conn: sqlite3.Connection,
) -> None:
    """THE two-window proof (§5.1(b) write vs §5.1(d) read).

    Two identical securities, one price row each for 6/5, differing ONLY in when the row
    was fetched:

    * ``PRE`` was fetched 6/8, before the 6/10 split — the provider delivered the price as
      traded (100), the write window ``(6/5, 6/8]`` is empty, and the row stores basis 1;
    * ``POST`` was fetched 6/25, after it — the provider re-stated its history and
      delivered 5, the write window ``(6/5, 6/25]`` contains the split, and the seam
      multiplies BACK UP to the same as-traded 100 with basis 20.

    Both then value 6/12 through the read rule, whose window ``(6/5, 6/12]`` also contains
    the split, and both must give **5** — the price in that day's share terms. An
    implementation that applied the write's factor again on the read (the "double
    application" this file's docstring warns about) would give 0.25 for POST and 5 for PRE:
    two different valuations of the same security, which is how the mistake announces
    itself.

    The second half then shows the windows are genuinely different sizes: valued on 6/15
    with a SECOND split on 6/20 in the ledger, the write factor is 10 (both splits were
    folded in by the 6/25 fetch) while the read factor is only 2 (one split has happened by
    6/15). Reusing the write's window would divide by 10 and understate by 80%.
    """
    split_20 = _action((Decimal("20"), Decimal("1"), date(2026, 6, 10)), symbol="PRE")
    upsert_prices(conn, [_row("100", sym="PRE")],
                  fetched_at=datetime(2026, 6, 8, 12, 0, 0), factor_of=_factor_of(split_20))
    post_split_20 = _action((Decimal("20"), Decimal("1"), date(2026, 6, 10)), symbol="POST")
    upsert_prices(conn, [_row("5", sym="POST")], fetched_at=_FETCHED,
                  factor_of=_factor_of(post_split_20))

    assert _close(conn, "PRE") == "100" and _close(conn, "POST") == "100"

    index = ActionIndex.build([split_20, post_split_20])
    valued = {
        sym: price_in(index, sym, Decimal(_close(conn, sym)),
                      priced_on=_DAY, valued_on=date(2026, 6, 12))
        for sym in ("PRE", "POST")
    }
    assert valued == {"PRE": Decimal("5"), "POST": Decimal("5")}

    # The windows are different SIZES, not the same product applied twice.
    second = _action((Decimal("5"), Decimal("1"), date(2026, 6, 20)), symbol="POST")
    two = ActionIndex.build([post_split_20, second])
    assert split_factor(two, "POST", after=_DAY, through=_FETCHED.date()) == Decimal("100")
    assert split_factor(two, "POST", after=_DAY, through=date(2026, 6, 15)) == Decimal("20")


def test_the_split_date_itself_is_already_in_post_split_terms(
    conn: sqlite3.Connection,
) -> None:
    """The half-open window, on BOTH seams — the classic off-by-one, locked.

    §4: an action is effective at the **start** of its date, and that day's trades are
    already quoted in post-action terms. Both windows are ``after < a.date <= through``,
    so a price dated ON the split date is excluded from both:

    * the WRITE leaves it at the provider's delivered value (it needs no un-adjusting);
    * the READ leaves it alone when valuing that same day.

    Shifting either bound by one day double-counts the split for exactly one trading day —
    the least visible possible version of this bug, and the one a manual check would miss.
    """
    split = _action((Decimal("20"), Decimal("1"), date(2026, 6, 10)))
    factor_of = _factor_of(split)
    on_the_day = date(2026, 6, 10)
    day_before = date(2026, 6, 9)
    upsert_prices(conn, [_row("5", d=on_the_day), _row("100", d=day_before)],
                  fetched_at=_FETCHED, factor_of=factor_of)
    # Write: the split-date row keeps the provider value; the day before is multiplied up.
    assert _close(conn, d=on_the_day) == "5" and _close(conn, d=day_before) == "2000"
    assert reconcile_prices(conn, ["AAPL"], factor_of=factor_of) == 0  # already correct

    index = ActionIndex.build([split])
    # Read, valuing the split date: its OWN price is untouched...
    assert price_in(index, "AAPL", Decimal("5"),
                    priced_on=on_the_day, valued_on=on_the_day) == Decimal("5")
    # ...while the previous day's as-traded price is divided into the same terms.
    assert price_in(index, "AAPL", Decimal("2000"),
                    priced_on=day_before, valued_on=on_the_day) == Decimal("100")
    # ...and valuing the day BEFORE the split needs no correction at all.
    assert price_in(index, "AAPL", Decimal("2000"),
                    priced_on=day_before, valued_on=day_before) == Decimal("2000")


# --- the application entry point W7 calls --------------------------------------------


def test_the_api_entry_point_reconciles_against_the_real_ledger(
    ledger_conn: sqlite3.Connection,
) -> None:
    """End-to-end through ``api.instrument_service.reconcile_price_basis`` and the real CRUD.

    This is the seam W7's router will call on every insert / edit / delete of a SPLIT row,
    and it is the only place the injected factor meets the actual ``corporate_actions``
    table. The three §7.1b behaviours are re-asserted here against real rows, because a
    reconcile that is correct with a hand-built factor and wired to the wrong ledger read
    is still a wrong number.
    """
    upsert_prices(ledger_conn, [_row("5")], fetched_at=_FETCHED)
    before = _snapshot(ledger_conn)
    assert before == [(_DAY.isoformat(), "5", "5", "1")]

    action_id = insert_corporate_action(
        ledger_conn, account_id="schwab", action_date=date(2026, 6, 10),
        kind=CorporateActionKind.SPLIT, from_symbol="AAPL", to_symbol="AAPL",
        ratio_to=Decimal("2"), ratio_from=Decimal("1"),
    )
    assert reconcile_price_basis(ledger_conn, ["AAPL"]) == 1
    assert _snapshot(ledger_conn) == [(_DAY.isoformat(), "10", "5", "2")]
    assert reconcile_price_basis(ledger_conn, ["AAPL"]) == 0  # idempotent

    update_corporate_action(
        ledger_conn, action_id, account_id="schwab", action_date=date(2026, 6, 10),
        kind=CorporateActionKind.SPLIT, from_symbol="AAPL", to_symbol="AAPL",
        ratio_to=Decimal("20"), ratio_from=Decimal("1"),
    )
    reconcile_price_basis(ledger_conn, ["AAPL"])
    assert _snapshot(ledger_conn) == [(_DAY.isoformat(), "100", "5", "20")]

    delete_corporate_action(ledger_conn, action_id)
    reconcile_price_basis(ledger_conn, ["AAPL"])
    assert _snapshot(ledger_conn) == before


def test_the_api_entry_point_is_a_no_op_for_an_empty_symbol_list(
    ledger_conn: sqlite3.Connection,
) -> None:
    """No symbols → no SELECT, no write. The caller decides the blast radius, and an
    EXCHANGE/SPINOFF router that correctly passes nothing must cost nothing."""
    upsert_prices(ledger_conn, [_row("5")], fetched_at=_FETCHED)
    before = _snapshot(ledger_conn)
    assert reconcile_price_basis(ledger_conn, []) == 0
    assert _snapshot(ledger_conn) == before
