"""W4 — the action-aware share walk: date bounds, per-kind rules, and the depth cap.

The parity test (``test_holdings_parity.py``) proves this walk agrees with ``build_book``.
These tests pin the rules ONE at a time, so a parity failure has an address — and each one
is paired with the mutation that makes it go red, because three of the four rules below were
found by an audit precisely as "a walker written from §6.2 alone gets this wrong".
"""

import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.holdings import (
    MAX_ACTION_DEPTH,
    _shares_until,
    current_shares,
    load_action_index,
    shares_on,
    shares_through,
)
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_transaction,
    load_ledger_bundle,
    upsert_instrument,
    upsert_opening,
)
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

D = Decimal
ACT = date(2024, 6, 10)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    c.execute(
        "INSERT INTO accounts (account_id, name, broker, settlement_ccy, funding_ccy, "
        "fee_rule_set, dividend_model) VALUES (?,?,?,?,?,?,?)",
        ("schwab", "Schwab", "Schwab", "USD", "TWD", "schwab_us", "drip_us"),
    )
    for sym in [f"S{i}" for i in range(60)] + ["AAPL", "PARENT", "CHILD", "OLD", "NEW"]:
        upsert_instrument(c, Instrument(symbol=sym, market=Market.US,
                                        quote_ccy=Currency.USD, sector="Tech", name=sym))
    yield c
    c.close()


def _split(conn: sqlite3.Connection, symbol: str, day: date, to: str, frm: str) -> None:
    insert_corporate_action(conn, account_id="schwab", action_date=day,
                            kind=CorporateActionKind.SPLIT, from_symbol=symbol,
                            to_symbol=symbol, ratio_to=D(to), ratio_from=D(frm))


# --------------------------------------------------------------- the three bounds


def test_opening_dated_on_the_action_date_is_pre_action(conn: sqlite3.Connection) -> None:
    """F-18 / D3, and the exact counter-example the audit measured.

    ``EventPriority.OPENING`` (0) precedes ``CORPORATE_ACTION`` (10), so an opening dated ON
    the action's date describes the position as it stood BEFORE — and the split must scale
    it. §6.2 states one bound (``< D``) for all three ledgers; under that bound this returns
    100 while the replay returns 300, which is why the walk needs its own event loader
    rather than ``_shares_until`` with a shifted ``before``.
    """
    upsert_opening(conn, account_id="schwab", symbol="AAPL", shares=D("100"),
                   original_cost_total=D("10000"), build_date=ACT)
    _split(conn, "AAPL", ACT, "3", "1")
    conn.commit()
    replayed = next(h for h in build_book(load_ledger_bundle(conn)).holdings
                    if h.symbol == "AAPL")
    assert replayed.shares == D("300")
    assert shares_through(conn, "schwab", "AAPL", on=ACT) == D("300")
    assert current_shares(conn, "schwab", "AAPL") == D("300")
    # …and the naive path, untouched, still says 100 — the divergence this package closes.
    assert _shares_until(conn, "schwab", "AAPL", None) == D("100")


def test_a_trade_dated_on_the_action_date_is_post_action(conn: sqlite3.Connection) -> None:
    """The other half of the same rule: BUY (20) sorts AFTER CORPORATE_ACTION (10).

    A same-day buy is quoted in post-action terms, so it is NOT scaled. Getting this the
    other way round is the mirror-image defect of F-18 and inflates the count instead.
    """
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=D("100"), price=D("10"), fees=D("0"), tax=D("0"),
                       trade_date=date(2024, 1, 2))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=D("30"), price=D("4"), fees=D("0"), tax=D("0"),
                       trade_date=ACT)
    _split(conn, "AAPL", ACT, "3", "1")
    conn.commit()
    assert shares_through(conn, "schwab", "AAPL", on=ACT) == D("330")   # 100×3 + 30
    replayed = next(h for h in build_book(load_ledger_bundle(conn)).holdings
                    if h.symbol == "AAPL")
    assert replayed.shares == D("330")


def test_shares_on_excludes_an_action_dated_on_that_day(conn: sqlite3.Connection) -> None:
    """``shares_on`` = "going INTO the date", so an action effective during it is excluded."""
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=D("100"), price=D("10"), fees=D("0"), tax=D("0"),
                       trade_date=date(2024, 1, 2))
    _split(conn, "AAPL", ACT, "3", "1")
    conn.commit()
    assert shares_on(conn, "schwab", "AAPL", before=ACT) == D("100")
    assert shares_through(conn, "schwab", "AAPL", on=ACT) == D("300")


def test_a_later_query_sees_the_action_and_an_earlier_one_does_not(
    conn: sqlite3.Connection,
) -> None:
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=D("100"), price=D("10"), fees=D("0"), tax=D("0"),
                       trade_date=date(2024, 1, 2))
    _split(conn, "AAPL", ACT, "3", "1")
    conn.commit()
    assert shares_through(conn, "schwab", "AAPL", on=date(2024, 6, 9)) == D("100")
    assert shares_through(conn, "schwab", "AAPL", on=date(2024, 6, 11)) == D("300")


# ------------------------------------------------------------------- per-kind rules


def test_a_spinoff_contributes_zero_to_its_own_source(conn: sqlite3.Connection) -> None:
    """F-26 — the rule §6.2 never states, and the one that looks half-right when wrong.

    The parent KEEPS its shares (§4.3); only cost is carved. A walker with a generic
    "source side" branch mirroring EXCHANGE zeroes the parent while the child stays correct.
    """
    insert_transaction(conn, account_id="schwab", symbol="PARENT", side=Side.BUY,
                       quantity=D("200"), price=D("10"), fees=D("0"), tax=D("0"),
                       trade_date=date(2024, 1, 2))
    insert_corporate_action(conn, account_id="schwab", action_date=ACT,
                            kind=CorporateActionKind.SPINOFF, from_symbol="PARENT",
                            to_symbol="CHILD", ratio_to=D("1"), ratio_from=D("4"),
                            cost_carry=D("0.25"))
    conn.commit()
    assert current_shares(conn, "schwab", "PARENT") == D("200")
    assert current_shares(conn, "schwab", "CHILD") == D("50")
    by_symbol = {h.symbol: h.shares for h in build_book(load_ledger_bundle(conn)).holdings}
    assert by_symbol == {"PARENT": D("200"), "CHILD": D("50")}


def test_an_exchange_empties_its_source_and_merges_into_its_destination(
    conn: sqlite3.Connection,
) -> None:
    for sym, qty, day in (("OLD", "40", date(2024, 1, 2)), ("NEW", "10", date(2024, 2, 2))):
        insert_transaction(conn, account_id="schwab", symbol=sym, side=Side.BUY,
                           quantity=D(qty), price=D("10"), fees=D("0"), tax=D("0"),
                           trade_date=day)
    insert_corporate_action(conn, account_id="schwab", action_date=ACT,
                            kind=CorporateActionKind.EXCHANGE, from_symbol="OLD",
                            to_symbol="NEW", ratio_to=D("1"), ratio_from=D("2"))
    conn.commit()
    assert current_shares(conn, "schwab", "OLD") == D("0")
    assert current_shares(conn, "schwab", "NEW") == D("30")


def test_a_split_is_applied_exactly_once(conn: sqlite3.Connection) -> None:
    """F-09 at the walker level: E20 makes a SPLIT its own source AND destination.

    Merging ``by_source`` with ``by_dest`` yields it twice and squares the ratio — 900 on a
    3-for-1 of 100, measured. ``for_symbol`` files it once at build time.
    """
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=D("100"), price=D("10"), fees=D("0"), tax=D("0"),
                       trade_date=date(2024, 1, 2))
    _split(conn, "AAPL", ACT, "3", "1")
    conn.commit()
    assert current_shares(conn, "schwab", "AAPL") == D("300")


def test_a_chain_resolves_transitively(conn: sqlite3.Connection) -> None:
    """The de-SPAC case: a destination's history reaches back through another symbol."""
    insert_transaction(conn, account_id="schwab", symbol="S0", side=Side.BUY,
                       quantity=D("100"), price=D("10"), fees=D("0"), tax=D("0"),
                       trade_date=date(2024, 1, 2))
    for i in range(9):
        insert_corporate_action(
            conn, account_id="schwab", action_date=date(2024, 2, 1 + i),
            kind=CorporateActionKind.EXCHANGE, from_symbol=f"S{i}", to_symbol=f"S{i + 1}",
            ratio_to=D("1"), ratio_from=D("1"))
    _split(conn, "S9", date(2024, 4, 1), "2", "1")
    conn.commit()
    assert current_shares(conn, "schwab", "S9") == D("200")
    assert current_shares(conn, "schwab", "S0") == D("0")


# ------------------------------------------------------------------- the depth cap


def test_the_depth_cap_degrades_and_flags_instead_of_hanging(
    conn: sqlite3.Connection,
) -> None:
    """D31: read paths keep the bare ``Decimal``; the capped position is RECORDED.

    The chain is longer than :data:`MAX_ACTION_DEPTH`, which validation makes unreachable —
    the cap exists for a hand-edited DB or a future relaxation of D15. What matters is the
    SHAPE of the failure: no hang, no partial sum, no ``Decimal | None`` for nine call sites
    to each invent a policy for, and no silent wrong number either.
    """
    insert_transaction(conn, account_id="schwab", symbol="S0", side=Side.BUY,
                       quantity=D("100"), price=D("10"), fees=D("0"), tax=D("0"),
                       trade_date=date(2024, 1, 2))
    deep = MAX_ACTION_DEPTH + 5
    for i in range(deep):
        insert_corporate_action(
            conn, account_id="schwab", action_date=date(2024, 2, 1) + timedelta(days=i),
            kind=CorporateActionKind.EXCHANGE, from_symbol=f"S{i}", to_symbol=f"S{i + 1}",
            ratio_to=D("1"), ratio_from=D("1"))
    conn.commit()
    index = load_action_index(conn)
    last = f"S{deep}"
    result = current_shares(conn, "schwab", last, index=index)
    assert isinstance(result, Decimal)                       # signature unchanged (D31)
    assert result == _shares_until(conn, "schwab", last, None)   # the pre-action fallback
    assert ("schwab", last) in index.depth_capped_symbols()


def test_a_chain_at_the_cap_is_still_computed(conn: sqlite3.Connection) -> None:
    """The cap must not fire early — insurance, not a limit on legitimate ledgers."""
    insert_transaction(conn, account_id="schwab", symbol="S0", side=Side.BUY,
                       quantity=D("100"), price=D("10"), fees=D("0"), tax=D("0"),
                       trade_date=date(2024, 1, 2))
    for i in range(MAX_ACTION_DEPTH - 2):
        insert_corporate_action(
            conn, account_id="schwab", action_date=date(2024, 2, 1) + timedelta(days=i),
            kind=CorporateActionKind.EXCHANGE, from_symbol=f"S{i}", to_symbol=f"S{i + 1}",
            ratio_to=D("1"), ratio_from=D("1"))
    conn.commit()
    index = load_action_index(conn)
    tail = f"S{MAX_ACTION_DEPTH - 2}"
    assert current_shares(conn, "schwab", tail, index=index) == D("100")
    assert index.depth_capped_symbols() == frozenset()
