"""R3 / QA-13 — the single FX write seam must refuse a non-positive rate.

``shared.fx.convert`` already refuses a non-positive rate ON READ
(``ValueError("FX rate must be positive and finite")``), but ``pricing.store.upsert_fx``
is the ONLY place FX rows are written and it stored whatever it was handed. The
consequences are asymmetric and both bad:

* a stored ``0`` makes an inverse read raise ``decimal.DivisionByZero`` — an exception
  class the dashboard's degradation catches (``except KeyError``) do not cover;
* a stored NEGATIVE rate is worse than either, because nothing raises at all: it
  sign-flips every converted figure silently, which is precisely the "never silently
  fabricate / never guess" red line in ``data-and-pricing.md``.

A bad value must therefore be refused where it is written, not survive to be discovered
by whichever reader happens to divide by it first. Filed as a missing guard, not as an
observed provider failure.
"""

import sqlite3
from datetime import date, datetime
from decimal import Decimal

import pytest

from portfolio_dash.pricing.results import FxRow
from portfolio_dash.pricing.store import get_fx, upsert_fx
from portfolio_dash.shared.enums import Currency

_NOW = datetime(2026, 6, 8, 12, 0, 0)
_AS_OF = date(2026, 6, 8)


def _fx(rate: str) -> FxRow:
    return FxRow(base=Currency.USD, quote=Currency.TWD, as_of=_AS_OF,
                 rate=Decimal(rate), source="test")


def _stored(conn: sqlite3.Connection) -> list[str]:
    return [r["rate"] for r in conn.execute("SELECT rate FROM fx_rates")]


def test_upsert_fx_rejects_a_zero_rate(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="FX rate"):
        upsert_fx(conn, [_fx("0")], fetched_at=_NOW)
    assert _stored(conn) == []


def test_upsert_fx_rejects_a_negative_rate(conn: sqlite3.Connection) -> None:
    """The silent one: a negative rate sign-flips every converted figure and never raises."""
    with pytest.raises(ValueError, match="FX rate"):
        upsert_fx(conn, [_fx("-32.5")], fetched_at=_NOW)
    assert _stored(conn) == []
    # And nothing reached the reader, so no consumer can convert money by it.
    assert get_fx(conn, Currency.USD, Currency.TWD, now=_NOW) is None


def test_upsert_fx_rejects_the_whole_batch_when_one_row_is_bad(
    conn: sqlite3.Connection,
) -> None:
    """Validated BEFORE the write, so a mixed batch never lands half-applied."""
    good = FxRow(base=Currency.USD, quote=Currency.MYR, as_of=_AS_OF,
                 rate=Decimal("4.4"), source="test")
    with pytest.raises(ValueError, match="FX rate"):
        upsert_fx(conn, [good, _fx("0")], fetched_at=_NOW)
    assert _stored(conn) == []


def test_upsert_fx_names_the_pair_and_the_value_it_refused(
    conn: sqlite3.Connection,
) -> None:
    """A refusal the operator cannot act on is a crash with better manners."""
    with pytest.raises(ValueError) as exc:
        upsert_fx(conn, [_fx("-32.5")], fetched_at=_NOW)
    message = str(exc.value)
    assert "USD" in message and "TWD" in message
    assert "2026-06-08" in message and "-32.5" in message


def test_upsert_fx_still_stores_an_ordinary_positive_rate(
    conn: sqlite3.Connection,
) -> None:
    """Control: the guard rejects only what ``shared.fx.convert`` would reject on read."""
    upsert_fx(conn, [_fx("32.457812")], fetched_at=_NOW)
    read = get_fx(conn, Currency.USD, Currency.TWD, now=_NOW)
    assert read is not None and read.rate == Decimal("32.457812")
