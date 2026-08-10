"""The two-column price basis at the write seam (spec §5.1(b)/(c), D30 · W6a).

``prices`` stores the provider's close as delivered (``close_raw``), the factor applied
to it (``split_basis``), and their product (``close``). These tests pin the three things
that were each measured to go wrong the obvious way:

* the 4-dp cap applied to the RAW value before the multiply instead of to the product
  (F-20) — a 0.025% error at ×20 and the sub-RM1 MY ETF case at ×3;
* ``ON CONFLICT DO UPDATE`` restating the close but not the basis (F-23) — silent, and
  only on a re-fetch;
* a non-integral identity factor silently repainting every stored price's TEXT (owner
  requirement 2026-08-10) — ``Decimal`` multiplication sums exponents.

The load-bearing assertions are on the stored **TEXT**, not on ``Decimal`` values:
``Decimal("1.5") == Decimal("1.50")`` is True, so value equality cannot see the defect
this package must not have.
"""

import sqlite3
from datetime import date, datetime
from decimal import Decimal

from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import (
    SplitFactorFn,
    _cap_dp,
    get_latest_price,
    upsert_prices,
)
from portfolio_dash.shared.enums import Market

_NOW = datetime(2026, 6, 8, 12, 0, 0)
_DAY = date(2026, 6, 5)

# A real yfinance float tail: the value a float-sourced provider actually delivers.
_FLOAT_TAIL = "0.14166666865348816"


def _row(close: str, d: date = _DAY, sym: str = "AAPL") -> PriceRow:
    return PriceRow(instrument=sym, market=Market.US, as_of=d, close=Decimal(close),
                    source="fake")


def _stored(conn: sqlite3.Connection, sym: str = "AAPL", d: date = _DAY) -> sqlite3.Row:
    row: sqlite3.Row | None = conn.execute(
        "SELECT close, close_raw, split_basis, open, high, low FROM prices "
        "WHERE instrument=? AND as_of_date=?", (sym, d.isoformat()),
    ).fetchone()
    assert row is not None
    return row


def _factor(value: str) -> SplitFactorFn:
    """A ``SplitFactorFn`` returning a fixed factor, ignoring the window."""
    def factor_of(symbol: str, *, after: date, through: date) -> Decimal:
        return Decimal(value)
    return factor_of


# --- the no-op guarantee ------------------------------------------------------------
# "A symbol with no corporate action must behave exactly as it did before this feature
# existed" — byte-identical, so a defect in the new flow can only reach the symbols that
# actually had an action.

# Mixed trailing zeros on purpose: each one is a DIFFERENT canonical string that a
# value-equality assertion would happily accept as unchanged.
_MIXED = ["1.5", "1.50", "305.3650", "0.005", "600", "0.4250", _FLOAT_TAIL]


def test_no_factor_stores_byte_identical_text(conn: sqlite3.Connection) -> None:
    """The default (no injection) writes the pre-feature bytes for every scale."""
    rows = [_row(v, date(2026, 6, i + 1)) for i, v in enumerate(_MIXED)]
    upsert_prices(conn, rows, fetched_at=_NOW)
    stored = [r["close"] for r in conn.execute(
        "SELECT close FROM prices WHERE instrument='AAPL' ORDER BY as_of_date")]
    # The pre-feature expression, evaluated independently of the seam.
    assert stored == [str(_cap_dp(Decimal(v), 4)) for v in _MIXED]
    assert stored == ["1.5", "1.50", "305.3650", "0.005", "600", "0.4250", "0.1417"]


def test_no_factor_records_the_identity_basis(conn: sqlite3.Connection) -> None:
    """``split_basis`` is the literal ``'1'`` — the same bytes the DDL default gives a
    legacy row — so a no-action row is indistinguishable from a never-migrated one."""
    upsert_prices(conn, [_row("100")], fetched_at=_NOW)
    row = _stored(conn)
    assert row["split_basis"] == "1" and row["close"] == "100" and row["close_raw"] == "100"


def test_identity_factor_with_a_scale_does_not_repaint(conn: sqlite3.Connection) -> None:
    """A factor of ``Decimal("1.0")`` must not add a decimal place to anything.

    ``Decimal`` multiplication sums the operands' EXPONENTS, so ``1.5 × 1.0`` is
    ``Decimal("1.50")`` — value-preserving, TEXT-changing — and ``_cap_dp`` does not
    catch it (the cap fires only BELOW 4 dp and never trims). Measured against this
    module's own helpers: ``600 → 600.0`` and ``0.005 → 0.0050``. Because Decimals
    persist as canonical TEXT, computing the identity instead of short-circuiting past
    it would rewrite every price row in the database on the next refresh — on symbols
    with no corporate action at all.
    """
    rows = [_row(v, date(2026, 6, i + 1)) for i, v in enumerate(_MIXED)]
    upsert_prices(conn, rows, fetched_at=_NOW, factor_of=_factor("1.0"))
    stored = [r["close"] for r in conn.execute(
        "SELECT close FROM prices WHERE instrument='AAPL' ORDER BY as_of_date")]
    # Computing the identity instead would give 1.50 / 1.500 / 0.0050 / 600.0 here.
    assert stored == ["1.5", "1.50", "305.3650", "0.005", "600", "0.4250", "0.1417"]
    # ...and the basis is normalised to the canonical identity, never "1.0".
    bases = {r["split_basis"] for r in conn.execute(
        "SELECT split_basis FROM prices WHERE instrument='AAPL'")}
    assert bases == {"1"}


# --- F-20: the cap goes LAST, on the product ----------------------------------------


def test_cap_is_applied_last_to_the_product(conn: sqlite3.Connection) -> None:
    """``_cap_dp(raw × target, 4)``, never ``_cap_dp(raw, 4) × target``.

    Capping first amplifies the cap's own error by the factor. The minimal edit — wrap
    the multiply around the existing ``to_db(_cap_dp(r.close, _PRICE_DP))`` — picks the
    wrong order, so this test fails on the natural implementation.
    """
    upsert_prices(conn, [_row(_FLOAT_TAIL)], fetched_at=_NOW,
                  factor_of=_factor("20"))
    assert _stored(conn)["close"] == "2.8333"  # cap-first would store 2.8340


def test_cap_last_at_the_my_sub_rm1_scale(conn: sqlite3.Connection) -> None:
    """The ×3 case: 0.4250 correct vs 0.4251 cap-first — a Bursa sub-RM1 ETF tick
    (``data-and-pricing.md`` singles out 3-dp MY prices; the 4-dp cap covers them)."""
    upsert_prices(conn, [_row(_FLOAT_TAIL)], fetched_at=_NOW, factor_of=_factor("3"))
    assert _stored(conn)["close"] == "0.4250"  # cap-first would store 0.4251


def test_stored_raw_reproduces_the_stored_close(conn: sqlite3.Connection) -> None:
    """``close == _cap_dp(close_raw × split_basis, 4)`` read back from the DB.

    This is the write/reconcile agreement property, and it is why ``close_raw`` is the
    one price column stored UN-capped: W6b's reconcile recomputes the close from this
    stored value, so it must be the same input the write used. Capping ``close_raw``
    here would make the first reconcile silently restate every price onto the cap-first
    value this package exists to reject.
    """
    upsert_prices(conn, [_row(_FLOAT_TAIL)], fetched_at=_NOW,
                  factor_of=_factor("20"))
    row = _stored(conn)
    assert row["close_raw"] == _FLOAT_TAIL  # full source precision, no cap
    rebuilt = _cap_dp(Decimal(row["close_raw"]) * Decimal(row["split_basis"]), 4)
    assert str(rebuilt) == row["close"]


# --- F-23: DO UPDATE must restate the basis columns ---------------------------------


def test_refetch_restates_every_basis_column(conn: sqlite3.Connection) -> None:
    """A re-fetch is a full restatement, not a partial one.

    Leaving ``close_raw`` / ``split_basis`` out of ``ON CONFLICT DO UPDATE`` leaves a row
    whose close was restated over a stale basis — so the NEXT reconcile rebuilds from the
    wrong starting point and compounds the error. Silent; only bites on a re-fetch.
    """
    upsert_prices(conn, [_row("100")], fetched_at=_NOW)  # first fetch: no action yet
    assert _stored(conn)["split_basis"] == "1"
    # the owner enters a 20-for-1; the next refresh re-fetches the same session
    upsert_prices(conn, [_row("5")], fetched_at=_NOW,
                  factor_of=_factor("20"))
    row = _stored(conn)
    assert (row["close"], row["close_raw"], row["split_basis"]) == ("100", "5", "20")
    assert len(list(conn.execute("SELECT 1 FROM prices"))) == 1  # still idempotent


# --- the injected window (F-22) -----------------------------------------------------


def test_factor_receives_the_rows_own_window(conn: sqlite3.Connection) -> None:
    """``after`` is the row's own ``as_of``; ``through`` is the ``fetched_at`` WRITTEN to
    that row — bound from the same argument, so the two can never disagree."""
    seen: list[tuple[str, date, date]] = []

    def spy(symbol: str, *, after: date, through: date) -> Decimal:
        seen.append((symbol, after, through))
        return Decimal(1)

    upsert_prices(conn, [_row("100", date(2026, 6, 4)), _row("101", date(2026, 6, 5))],
                  fetched_at=_NOW, factor_of=spy)
    assert seen == [("AAPL", date(2026, 6, 4), _NOW.date()),
                    ("AAPL", date(2026, 6, 5), _NOW.date())]


# --- the columns nobody reads -------------------------------------------------------


def test_ohlc_keeps_the_provider_basis(conn: sqlite3.Connection) -> None:
    """``open``/``high``/``low`` are NOT multiplied, deliberately (see ``upsert_prices``).

    They have no reader in the codebase and no raw column of their own, so multiplying
    them would create a derived value W6b's reconcile could never restate. ``split_basis``
    on the same row records the difference, so nothing is lost. Locked by a test so that
    giving them the factor is a deliberate change that also adds their raws — the same
    treatment §5.1 already gives ``volume``.
    """
    row_with_ohlc = PriceRow(instrument="AAPL", market=Market.US, as_of=_DAY,
                             close=Decimal("5"), open=Decimal("4"), high=Decimal("6"),
                             low=Decimal("3"), source="fake")
    upsert_prices(conn, [row_with_ohlc], fetched_at=_NOW, factor_of=_factor("20"))
    row = _stored(conn)
    assert row["close"] == "100"  # re-expressed
    assert (row["open"], row["high"], row["low"]) == ("4", "6", "3")  # provider basis


def test_reads_are_unaffected_by_the_new_columns(conn: sqlite3.Connection) -> None:
    """``get_latest_price`` keeps serving the re-expressed close (the read column)."""
    upsert_prices(conn, [_row("5")], fetched_at=_NOW,
                  factor_of=_factor("20"))
    r = get_latest_price(conn, "AAPL", now=_NOW)
    assert r is not None and r.value == Decimal("100")
