"""QA-05 — a typed SPINOFF child price must survive a LATER split of the child.

``_seed_child_price`` (D48b) writes the child's opening price through the ONE price write
seam, dated the action day. It used to stamp that row with ``fetched_at = now``, justified
by 「the child is brand new, so no split can exist between that date and now」 — a derivation
from the child's *age*, and false for a **back-dated** spin-off, which the app fully permits
and which backfilling broker history is the stated use case for.

The consequence is measured below. ``pricing/reconcile.reconcile_prices`` restates every
stored row over the window ``(as_of_date, fetched_at]`` — "which splits had the provider
already folded into this delivered number?" — and multiplies them back OUT to recover the
as-traded value. With ``fetched_at = now``, a split recorded years after the spin-off falls
inside that window, so a hand-typed 50.00 is stored as ``close='100.00'`` over
``close_raw='50.00'`` / ``split_basis='2'``. The read (``portfolio/price_basis.price_in``)
then divides the same split back out, landing on 50.00 again — against a **post-split**
100-share count. 5,000 instead of 2,500: the position doubles, silently, on the split date.

The owner typed an **as-traded** price and dated it themselves, so it was "observed" on its
own date: the row is written with ``fetched_at`` on the action day, which makes the write
window AND every later reconcile window ``(as_of, fetched_at]`` empty **by construction**
(``as_of`` IS the action day for this row), so ``split_basis`` can only ever be the identity.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

_BASE = "/api/ledgers/corporate-actions"
_SPINOFF_DAY = date(2023, 1, 15)
_SPLIT_DAY = date(2024, 6, 1)


def _seed_parent(conn: sqlite3.Connection) -> None:
    """100 PARN bought in 2022; CHLD registered but never traded and never priced."""
    for symbol, name in (("PARN", "Parent"), ("CHLD", "Child")):
        upsert_instrument(conn, Instrument(symbol=symbol, market=Market.US,
                                           quote_ccy=Currency.USD, sector="Tech",
                                           name=name))
    insert_transaction(conn, account_id="schwab", symbol="PARN", side=Side.BUY,
                       quantity=Decimal("100"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2022, 1, 10))
    conn.commit()


def _spinoff_body() -> dict[str, object]:
    """1-for-2 spin-off: 100 PARN -> 50 CHLD, 20% of the cost carried, child at 50.00."""
    return {"account_id": "schwab", "date": _SPINOFF_DAY.isoformat(), "kind": "SPINOFF",
            "from_symbol": "PARN", "to_symbol": "CHLD",
            "ratio_to": "1", "ratio_from": "2", "cost_carry": "0.2",
            "to_symbol_price": "50.00", "ack_warnings": True}


def _split_body() -> dict[str, object]:
    """The child's own 2-for-1 split, a year and a half AFTER the spin-off."""
    return {"account_id": "schwab", "date": _SPLIT_DAY.isoformat(), "kind": "SPLIT",
            "from_symbol": "CHLD", "to_symbol": "CHLD",
            "ratio_to": "2", "ratio_from": "1", "ack_warnings": True}


def _child_row(conn: sqlite3.Connection) -> sqlite3.Row:
    row: sqlite3.Row | None = conn.execute(
        "SELECT as_of_date, fetched_at, close, close_raw, split_basis, source "
        "FROM prices WHERE instrument='CHLD'").fetchone()
    assert row is not None, "the typed child price was not stored at all"
    return row


def _holding(client: TestClient, symbol: str) -> dict[str, object]:
    r = client.get("/api/dashboard")
    assert r.status_code == 200, r.text
    for h in r.json()["holdings"]:
        if h["symbol"] == symbol:
            return dict(h)
    raise AssertionError(f"{symbol} is not in the dashboard holdings")


def test_the_typed_child_price_is_stamped_as_observed_on_the_action_day(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """``fetched_at`` on the action date is what makes every reconcile window empty.

    Asserted on the STORED value rather than on its effect, because the effect (the next
    test) only appears once a later split exists — and a row written with today's stamp
    looks perfectly correct until then.
    """
    _seed_parent(golden_db)
    assert api_client.post(_BASE, json=_spinoff_body()).status_code == 201
    row = _child_row(golden_db)
    assert row["as_of_date"] == _SPINOFF_DAY.isoformat()
    assert row["fetched_at"].startswith(_SPINOFF_DAY.isoformat())
    assert (row["close"], row["close_raw"], row["split_basis"]) == ("50.00", "50.00", "1")


def test_a_later_split_of_the_child_does_not_restate_the_typed_price(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """★ The defect: the child's own split multiplied a price that never contained it."""
    _seed_parent(golden_db)
    assert api_client.post(_BASE, json=_spinoff_body()).status_code == 201
    before = _child_row(golden_db)
    r = api_client.post(_BASE, json=_split_body())
    assert r.status_code == 201, r.text
    after = _child_row(golden_db)
    assert (after["close"], after["split_basis"]) == ("50.00", "1")
    # And byte-identically so: a restatement that changes only the TEXT is still a
    # restatement (D38 invariant 3).
    assert (after["close"], after["close_raw"], after["split_basis"]) == (
        before["close"], before["close_raw"], before["split_basis"])


def test_the_child_is_valued_at_the_split_adjusted_price_against_split_adjusted_shares(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """★ The money-of-record consequence, end to end.

    ORACLE (hand-derived): the spin-off gives 100 PARN / 2 = **50 CHLD** at the typed
    **50.00** as traded on 2023-01-15 -> 2,500. A 2-for-1 split changes the denomination,
    never the value: **100 CHLD** at **25.00** -> the SAME 2,500. ``price_in`` divides the
    stored as-traded 50.00 by the split factor 2 for a valuation day after 2024-06-01.
    """
    _seed_parent(golden_db)
    assert api_client.post(_BASE, json=_spinoff_body()).status_code == 201
    assert api_client.post(_BASE, json=_split_body()).status_code == 201
    child = _holding(api_client, "CHLD")
    assert Decimal(str(child["shares"])) == Decimal("100")
    assert Decimal(str(child["market_price"])) == Decimal("25")
    assert Decimal(str(child["market_value"])) == Decimal("2500")


def test_the_child_is_valued_at_the_typed_price_before_any_split(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """Control: with no split recorded, the same ledger values the child at 50 x 50."""
    _seed_parent(golden_db)
    assert api_client.post(_BASE, json=_spinoff_body()).status_code == 201
    child = _holding(api_client, "CHLD")
    assert Decimal(str(child["shares"])) == Decimal("50")
    assert Decimal(str(child["market_price"])) == Decimal("50")
    assert Decimal(str(child["market_value"])) == Decimal("2500")
