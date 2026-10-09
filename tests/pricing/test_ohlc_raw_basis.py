"""W8: ``open`` / ``high`` / ``low`` get the same two-column basis the close has.

Until now only ``close`` was re-expressed. ``open``/``high``/``low`` were stored exactly as
the provider delivered them, which meant a row could carry a post-split close beside
pre-split highs and lows — the schema comment said so, and the rule file said the first
candlestick drawn from this table would have to divide them by ``split_basis`` itself.

That was a defensible position while the columns had **no reader anywhere** (verified again
2026-08-27: the only statement naming them is the INSERT). It is a trap all the same, and the
rule that documented it named its own remedy — "or add its own raw columns". Owner ruling
2026-08-27 (spec ``AI-D58``): add them.

The load-bearing assertions are on stored **TEXT**, not on ``Decimal`` values, for the same
reason ``test_split_basis.py`` gives: ``Decimal("1.5") == Decimal("1.50")`` is True, so value
equality cannot see a repaint.
"""

import sqlite3
from datetime import date, datetime
from decimal import Decimal

from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import SplitFactorFn, upsert_prices
from portfolio_dash.shared.enums import Market

_NOW = datetime(2026, 6, 8, 12, 0, 0)
_DAY = date(2026, 6, 5)
_FLOAT_TAIL = "0.14166666865348816"     # a real yfinance float tail


def _row(*, close: str, open_: str | None = None, high: str | None = None,
         low: str | None = None, d: date = _DAY) -> PriceRow:
    return PriceRow(
        instrument="AAPL", market=Market.US, as_of=d, close=Decimal(close),
        open=None if open_ is None else Decimal(open_),
        high=None if high is None else Decimal(high),
        low=None if low is None else Decimal(low),
        source="fake",
    )


def _stored(conn: sqlite3.Connection, d: date = _DAY) -> sqlite3.Row:
    row: sqlite3.Row | None = conn.execute(
        "SELECT close, close_raw, open, open_raw, high, high_raw, low, low_raw, "
        "split_basis FROM prices WHERE instrument='AAPL' AND as_of_date=?",
        (d.isoformat(),),
    ).fetchone()
    assert row is not None
    return row


def _factor(value: str) -> SplitFactorFn:
    def factor_of(symbol: str, *, after: date, through: date) -> Decimal:
        return Decimal(value)
    return factor_of


def test_every_ohlc_column_is_expressed_under_the_same_basis(
    conn: sqlite3.Connection
) -> None:
    upsert_prices(conn, [_row(close="10", open_="9", high="11", low="8")],
                  fetched_at=_NOW, factor_of=_factor("4"))
    r = _stored(conn)
    assert r["split_basis"] == "4"
    # One factor, one row: all four move together or the row is internally inconsistent.
    assert (r["close"], r["open"], r["high"], r["low"]) == ("40", "36", "44", "32")


def test_each_raw_column_keeps_the_providers_value_uncapped(
    conn: sqlite3.Connection
) -> None:
    """``*_raw`` is the input the reconcile recomputes from, so it must not lose precision.

    Capping it would make the first reconcile silently move the derived value — the same
    reason ``close_raw`` has always been exempt from the 4-dp cap.
    """
    upsert_prices(conn, [_row(close=_FLOAT_TAIL, open_=_FLOAT_TAIL,
                              high=_FLOAT_TAIL, low=_FLOAT_TAIL)],
                  fetched_at=_NOW, factor_of=_factor("20"))
    r = _stored(conn)
    for column in ("close_raw", "open_raw", "high_raw", "low_raw"):
        assert r[column] == _FLOAT_TAIL, column
    # And the cap goes LAST, on the product (F-20): cap(raw)×20 would store 2.8340.
    for column in ("close", "open", "high", "low"):
        assert r[column] == "2.8333", column


def test_a_missing_column_stays_missing_in_both_places(conn: sqlite3.Connection) -> None:
    """Providers routinely omit OHLC. A factor must not conjure a value out of NULL."""
    upsert_prices(conn, [_row(close="10")], fetched_at=_NOW, factor_of=_factor("4"))
    r = _stored(conn)
    assert r["close"] == "40"
    for column in ("open", "open_raw", "high", "high_raw", "low", "low_raw"):
        assert r[column] is None, column


def test_no_factor_stores_the_providers_bytes_for_every_column(
    conn: sqlite3.Connection
) -> None:
    """The identity must be the untouched pre-existing expression, not a multiply by one.

    ``Decimal`` multiplication sums exponents, so ``× Decimal("1.0")`` would rewrite ``600``
    as ``600.0`` — value-preserving, TEXT-changing, and it would repaint every row on
    symbols that never had a corporate action.
    """
    upsert_prices(conn, [_row(close="600", open_="1.50", high="0.005", low="0.4250")],
                  fetched_at=_NOW)
    r = _stored(conn)
    assert r["split_basis"] == "1"
    assert (r["close"], r["open"], r["high"], r["low"]) == ("600", "1.50", "0.005", "0.4250")


def test_a_refetch_restates_every_basis_column(conn: sqlite3.Connection) -> None:
    """F-23, applied to the three new columns: DO UPDATE must not leave a stale basis."""
    upsert_prices(conn, [_row(close="10", open_="9", high="11", low="8")],
                  fetched_at=_NOW, factor_of=_factor("4"))
    upsert_prices(conn, [_row(close="10", open_="9", high="11", low="8")],
                  fetched_at=_NOW, factor_of=_factor("1"))
    r = _stored(conn)
    assert r["split_basis"] == "1"
    assert (r["close"], r["open"], r["high"], r["low"]) == ("10", "9", "11", "8")


def test_an_existing_row_without_raw_columns_is_backfilled_from_itself(
    conn: sqlite3.Connection
) -> None:
    """Migration: a pre-W8 row has no ``*_raw``. Its basis is 1, so its raw IS its value.

    Stated by the migration rather than left NULL for a later reconcile to guess at —
    exactly what ``close_raw``'s own backfill does.
    """
    from portfolio_dash.pricing.schema import create_tables

    conn.execute(
        "INSERT INTO prices (instrument, market, as_of_date, close, open, high, low, "
        "source, fetched_at, close_raw, split_basis) "
        "VALUES ('OLD','US','2026-01-02','10','9','11','8','fake',?,'10','1')",
        (_NOW.isoformat(),),
    )
    conn.execute("UPDATE prices SET open_raw=NULL, high_raw=NULL, low_raw=NULL "
                 "WHERE instrument='OLD'")
    conn.commit()
    create_tables(conn)                      # idempotent — re-runs the migration
    row = conn.execute(
        "SELECT open_raw, high_raw, low_raw FROM prices WHERE instrument='OLD'"
    ).fetchone()
    assert (row["open_raw"], row["high_raw"], row["low_raw"]) == ("9", "11", "8")


def test_a_deleted_split_restores_every_column_byte_identically(
    conn: sqlite3.Connection
) -> None:
    """Reversibility, which is the property that made W8 safe to do at all.

    ``prices`` is the only place the corporate-action feature writes outside the ledgers,
    and 重算 does not rebuild it — so a SPLIT inserted and then deleted has to leave the
    table exactly as it found it. Recomputing from ``*_raw`` gives that by construction;
    rescaling in place, or dividing the old basis back out, would not.
    """
    from portfolio_dash.pricing.reconcile import reconcile_prices

    upsert_prices(conn, [_row(close="10", open_="9", high="11", low="8")],
                  fetched_at=_NOW)
    before = dict(_stored(conn))

    # A SPLIT appears: every column is restated under the new factor.
    assert reconcile_prices(conn, ["AAPL"], factor_of=_factor("4")) == 1
    during = _stored(conn)
    assert (during["close"], during["open"], during["high"], during["low"]) == (
        "40", "36", "44", "32")
    assert during["split_basis"] == "4"

    # The SPLIT is deleted: the factor returns to the identity and so does the row.
    assert reconcile_prices(conn, ["AAPL"], factor_of=_factor("1")) == 1
    assert dict(_stored(conn)) == before, "the row did not come back byte-identical"


def test_the_reconcile_leaves_an_already_correct_row_untouched(
    conn: sqlite3.Connection
) -> None:
    """Idempotency: a second pass must report zero, not rewrite identical bytes."""
    from portfolio_dash.pricing.reconcile import reconcile_prices

    upsert_prices(conn, [_row(close="10", open_="9", high="11", low="8")],
                  fetched_at=_NOW, factor_of=_factor("4"))
    assert reconcile_prices(conn, ["AAPL"], factor_of=_factor("4")) == 0
