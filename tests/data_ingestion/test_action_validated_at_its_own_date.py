"""The four book-derived rejections must read the ledger AT THE ACTION'S DATE.

**A live blocker, measured 2026-08-11 while building §10.5's acceptance script.**
``validate_corporate_action`` replayed the WHOLE ledger and four hard rejections read it —
**E3** (`oversold_source`), **E22** (`oversold_destination`), **E5** (`short_source`) and
**E18** (`short_destination`). On any ledger where the post-action trades are already
recorded — which is every bulk import, and therefore §10.5's acceptance run *by
construction* — the affected position is **already 賣超 when its action is validated**, so
E3 hard-rejects the very row that resolves the 賣超:

    AAA 目前是賣超（待釐清）部位，成本基礎已被捨棄，無法套用公司行動。
    請先補登缺少的買進或期初庫存

That is the system telling the owner to **fabricate a buy instead of recording the split**,
on the exact shape §1 says the feature exists for: buy 100, 7-for-1, sell 400.

Same class as **F-08** (E1a read the naive share count, so the second action of every chain
was uncommittable) and D13's 「the ⚠ provably never fires」: **a guard evaluated against a
state the action itself would fix.** It survived review because §6.7's **door 1** — the 賣超
confirm dialog — cannot exhibit it: there the sell is not committed yet, so the future the
guard wrongly reads does not exist. The primary door is the one door that hides it.

**The cut is the walker's, not a plain date.** F-18/D3 already settled the three bounds:
``opening <= D`` (a same-day opening is pre-action, ``EventPriority.OPENING = 0``) while
``transactions`` / ``dividends`` / ``actions`` are ``< D``. ``LedgerBundle.through`` is
``<= day`` on all four, so it is NOT the right filter here — a same-day sell would still be
replayed before the action that authorises it.

Nothing is weakened: ``_apply_action`` enforces all four rejections in true chronological
order on every replay, so a genuinely oversold position is still refused **when the replay
reaches the action**.
"""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.store import (
    insert_transaction,
    load_ledger_bundle,
    upsert_instrument,
    upsert_opening,
)
from portfolio_dash.data_ingestion.validate import (
    CorporateActionInput,
    Issue,
    validate_corporate_action,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

D = Decimal
SPLIT_DAY = date(2024, 6, 3)


def _hard(issues: list[Issue]) -> set[str]:
    return {i.kind for i in issues if not i.needs_confirm}


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    c.execute(
        "INSERT INTO accounts (account_id,name,broker,settlement_ccy,funding_ccy,"
        "fee_rule_set,dividend_model) VALUES "
        "('schwab','S','Schwab','USD','TWD','schwab_us','drip_us')"
    )
    for sym in ("AAA", "BBB", "CCC"):
        upsert_instrument(c, Instrument(symbol=sym, market=Market.US,
                                        quote_ccy=Currency.USD, sector="Tech", name=sym))
    return c


def _buy(conn: sqlite3.Connection, sym: str, qty: str, day: date, *,
         side: Side = Side.BUY, short: bool = False) -> None:
    insert_transaction(conn, account_id="schwab", symbol=sym, side=side, quantity=D(qty),
                       price=D("100"), fees=D("0"), tax=D("0"), trade_date=day,
                       short_sale=short)
    conn.commit()


def _split(**over: object) -> CorporateActionInput:
    base: dict[str, object] = {
        "account_id": "schwab", "date": SPLIT_DAY, "kind": "SPLIT",
        "from_symbol": "AAA", "to_symbol": "AAA", "ratio_to": D("7"), "ratio_from": D("1"),
    }
    base.update(over)
    return CorporateActionInput(**base)  # type: ignore[arg-type]


# --- the blocker itself -----------------------------------------------------------------


def test_the_split_that_resolves_an_oversell_is_accepted(conn: sqlite3.Connection) -> None:
    """§1's headline case: buy 100, 7-for-1, sell 400. The sell is legal ONLY after the
    split — so at import time the position is already 賣超, and E3 must not read that."""
    _buy(conn, "AAA", "100", date(2024, 1, 10))
    _buy(conn, "AAA", "400", date(2024, 9, 1), side=Side.SELL)
    inp = _split()
    assert _hard(validate_corporate_action(conn, inp, batch=[inp])) == set()


def test_e3_still_refuses_a_position_oversold_BEFORE_the_action(
    conn: sqlite3.Connection,
) -> None:
    """…and the guard must still bite, or the fix above is just a deletion.

    Here the oversell happens BEFORE the action date, so the basis really is discarded by
    the time the action lands and scaling an undefined basis is still undefined.
    """
    _buy(conn, "AAA", "100", date(2024, 1, 10))
    _buy(conn, "AAA", "400", date(2024, 2, 1), side=Side.SELL)   # before the split
    inp = _split()
    assert "oversold_source" in _hard(validate_corporate_action(conn, inp, batch=[inp]))


def test_e18_reads_the_destination_at_the_action_date_too(
    conn: sqlite3.Connection,
) -> None:
    """The destination short is a *later* event, so it must not block the exchange."""
    _buy(conn, "BBB", "50", date(2024, 1, 10))
    _buy(conn, "CCC", "10", date(2024, 1, 11))
    _buy(conn, "CCC", "80", date(2024, 12, 1), side=Side.SELL, short=True)  # after
    inp = _split(kind="EXCHANGE", from_symbol="BBB", to_symbol="CCC",
                 ratio_to=D("1"), ratio_from=D("1"))
    assert _hard(validate_corporate_action(conn, inp, batch=[inp])) == set()


def test_e5_still_refuses_a_short_open_ON_the_action_date(
    conn: sqlite3.Connection,
) -> None:
    """The other direction for the source: a short opened before the action is real."""
    _buy(conn, "BBB", "50", date(2024, 1, 10))
    _buy(conn, "BBB", "80", date(2024, 2, 1), side=Side.SELL, short=True)   # before
    inp = _split(kind="EXCHANGE", from_symbol="BBB", to_symbol="CCC",
                 ratio_to=D("1"), ratio_from=D("1"))
    _buy(conn, "CCC", "1", date(2024, 1, 5))
    assert "short_source" in _hard(validate_corporate_action(conn, inp, batch=[inp]))


# --- the cut is the walker's three bounds, not `<= day` ----------------------------------


def test_a_same_day_sell_is_NOT_replayed_before_the_action(
    conn: sqlite3.Connection,
) -> None:
    """`EventPriority`: CORPORATE_ACTION (10) runs before SELL (30) on the same date.

    A `through(day)` filter (`<= day` on every ledger) would include this sell and
    reproduce the blocker on exactly the dates where a broker books the split and the
    sale together. That is why the filter is the walker's THREE bounds, not one date.
    """
    _buy(conn, "AAA", "100", date(2024, 1, 10))
    _buy(conn, "AAA", "400", SPLIT_DAY, side=Side.SELL)     # SAME DAY as the split
    inp = _split()
    assert _hard(validate_corporate_action(conn, inp, batch=[inp])) == set()


def test_a_same_day_opening_IS_replayed_before_the_action(
    conn: sqlite3.Connection,
) -> None:
    """D3 / F-18: `EventPriority.OPENING = 0 < CORPORATE_ACTION = 10`, so an opening dated
    ON the action date is pre-action — the one ledger whose bound is `<=`, not `<`.

    Detection power: it must reach the book, not merely fail to break anything. An opening
    of 100 against a sell of 400 leaves the position 賣超 before the split, so E3 fires —
    and it fires ONLY if the same-day opening was included.
    """
    upsert_opening(conn, account_id="schwab", symbol="AAA", shares=D("100"),
                   original_cost_total=D("60000"), build_date=SPLIT_DAY)
    _buy(conn, "AAA", "400", date(2024, 4, 1), side=Side.SELL)   # BEFORE the split
    inp = _split()
    assert "oversold_source" in _hard(validate_corporate_action(conn, inp, batch=[inp]))


# --- containment -------------------------------------------------------------------------


def test_a_clean_ledger_is_unaffected(conn: sqlite3.Connection) -> None:
    """D38 invariant 1: no oversell, no short — the date scoping changes no verdict."""
    _buy(conn, "AAA", "100", date(2024, 1, 10))
    inp = _split()
    assert _hard(validate_corporate_action(conn, inp, batch=[inp])) == set()


def test_a_caller_supplied_bundle_is_still_honoured(conn: sqlite3.Connection) -> None:
    """The hoist survives: callers pass the BUNDLE (the expensive DB read) once, and the
    date scoping happens inside, so no caller can hand in a wrongly-scoped book."""
    _buy(conn, "AAA", "100", date(2024, 1, 10))
    _buy(conn, "AAA", "400", date(2024, 9, 1), side=Side.SELL)
    inp = _split()
    bundle = load_ledger_bundle(conn)
    assert _hard(validate_corporate_action(conn, inp, batch=[inp], bundle=bundle)) == set()
