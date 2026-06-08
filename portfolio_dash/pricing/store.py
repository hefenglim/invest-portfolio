"""Idempotent upsert + read for prices and FX rates.

Owns the only writes to the ``prices`` / ``fx_rates`` tables (per `pricing/`'s
responsibility in `architecture.md`). Upserts are idempotent on the natural key
(``instrument, as_of_date`` / ``base, quote, as_of_date``) via
``INSERT ... ON CONFLICT DO UPDATE`` — re-running a refresh never duplicates rows.

Reads return the latest-known value plus a ``stale`` flag (age vs. ``max_age_days``)
so the dashboard can degrade gracefully (`data-and-pricing.md`): serve last-known
data with a clear staleness indicator, never crash, never fabricate.
"""

import sqlite3
from datetime import date, datetime
from decimal import Decimal

from portfolio_dash.pricing.results import FxRead, FxRow, PriceRead, PriceRow
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.money import from_db, to_db

_DEFAULT_MAX_AGE = 4  # days


def _opt(v: Decimal | None) -> str | None:
    return to_db(v) if v is not None else None


def upsert_prices(conn: sqlite3.Connection, rows: list[PriceRow], *, fetched_at: datetime) -> None:
    """Upsert quote rows into ``prices``, keyed on (instrument, as_of_date)."""
    conn.executemany(
        """INSERT INTO prices (instrument, market, as_of_date, close, open, high, low,
               volume, source, fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(instrument, as_of_date) DO UPDATE SET
               close=excluded.close, open=excluded.open, high=excluded.high, low=excluded.low,
               volume=excluded.volume, source=excluded.source, fetched_at=excluded.fetched_at""",
        [(r.instrument, r.market.value, r.as_of.isoformat(), to_db(r.close), _opt(r.open),
          _opt(r.high), _opt(r.low), _opt(r.volume), r.source, fetched_at.isoformat())
         for r in rows],
    )
    conn.commit()


def get_latest_price(
    conn: sqlite3.Connection,
    instrument: str,
    *,
    now: datetime,
    max_age_days: int = _DEFAULT_MAX_AGE,
) -> PriceRead | None:
    """Return the most-recent stored price for ``instrument``, or ``None`` if absent.

    ``stale`` is ``True`` when the price's ``as_of`` date is more than ``max_age_days``
    days before ``now``'s date.
    """
    row = conn.execute(
        "SELECT close, as_of_date, source FROM prices WHERE instrument=? "
        "ORDER BY as_of_date DESC LIMIT 1",
        (instrument,),
    ).fetchone()
    if row is None:
        return None
    as_of = date.fromisoformat(row["as_of_date"])
    return PriceRead(
        value=from_db(row["close"]),
        as_of=as_of,
        source=row["source"],
        stale=(now.date() - as_of).days > max_age_days,
    )


def upsert_fx(conn: sqlite3.Connection, rows: list[FxRow], *, fetched_at: datetime) -> None:
    """Upsert FX rate rows into ``fx_rates``, keyed on (base, quote, as_of_date)."""
    conn.executemany(
        """INSERT INTO fx_rates (base, quote, as_of_date, rate, source, fetched_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(base, quote, as_of_date) DO UPDATE SET
               rate=excluded.rate, source=excluded.source, fetched_at=excluded.fetched_at""",
        [(r.base.value, r.quote.value, r.as_of.isoformat(), to_db(r.rate), r.source,
          fetched_at.isoformat()) for r in rows],
    )
    conn.commit()


def get_fx(
    conn: sqlite3.Connection,
    base: Currency,
    quote: Currency,
    *,
    now: datetime,
    max_age_days: int = _DEFAULT_MAX_AGE,
) -> FxRead | None:
    """Return the most-recent stored FX rate for ``base``/``quote``, or ``None`` if absent.

    ``stale`` is ``True`` when the rate's ``as_of`` date is more than ``max_age_days``
    days before ``now``'s date.
    """
    row = conn.execute(
        "SELECT rate, as_of_date, source FROM fx_rates WHERE base=? AND quote=? "
        "ORDER BY as_of_date DESC LIMIT 1",
        (base.value, quote.value),
    ).fetchone()
    if row is None:
        return None
    as_of = date.fromisoformat(row["as_of_date"])
    return FxRead(
        rate=from_db(row["rate"]),
        as_of=as_of,
        source=row["source"],
        stale=(now.date() - as_of).days > max_age_days,
    )
