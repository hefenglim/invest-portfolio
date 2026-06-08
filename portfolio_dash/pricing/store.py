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

from portfolio_dash.pricing.results import DividendEvent, FxRead, FxRow, PriceRead, PriceRow
from portfolio_dash.shared.enums import Currency, Market
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


def get_price_history(
    conn: sqlite3.Connection, instrument: str, start: date, end: date,
) -> list[PriceRead]:
    """Return stored daily prices for ``instrument`` within ``[start, end]``, ascending.

    Used for historical backfill reads (Phase B). Unlike `get_latest_price`, this
    returns a full series and does not compute staleness (``stale`` is always
    ``False`` — staleness is a latest-quote concern).
    """
    rows = conn.execute(
        "SELECT close, as_of_date, source FROM prices WHERE instrument=? "
        "AND as_of_date BETWEEN ? AND ? ORDER BY as_of_date ASC",
        (instrument, start.isoformat(), end.isoformat()),
    ).fetchall()
    return [
        PriceRead(value=from_db(r["close"]), as_of=date.fromisoformat(r["as_of_date"]),
                  source=r["source"], stale=False)
        for r in rows
    ]


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


def upsert_dividend_events(
    conn: sqlite3.Connection, events: list[DividendEvent], *, fetched_at: datetime,
) -> None:
    """Upsert dividend reference-data rows into ``dividend_events``.

    Keyed on the natural key (``instrument, ex_date``) — re-running a refresh never
    duplicates rows.
    """
    conn.executemany(
        """INSERT INTO dividend_events (instrument, market, ex_date, pay_date, cash_amount,
               stock_amount, currency, source, fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(instrument, ex_date) DO UPDATE SET
               pay_date=excluded.pay_date, cash_amount=excluded.cash_amount,
               stock_amount=excluded.stock_amount, currency=excluded.currency,
               source=excluded.source, fetched_at=excluded.fetched_at""",
        [(e.instrument, e.market.value, e.ex_date.isoformat(),
          e.pay_date.isoformat() if e.pay_date is not None else None,
          _opt(e.cash_amount), _opt(e.stock_amount),
          e.currency.value if e.currency is not None else None, e.source,
          fetched_at.isoformat()) for e in events],
    )
    conn.commit()


def get_dividend_events(conn: sqlite3.Connection, instrument: str) -> list[DividendEvent]:
    """Return stored dividend events for ``instrument``, ascending by ex-date."""
    rows = conn.execute(
        "SELECT instrument, market, ex_date, pay_date, cash_amount, stock_amount, currency, "
        "source FROM dividend_events WHERE instrument=? ORDER BY ex_date ASC",
        (instrument,)).fetchall()
    return [
        DividendEvent(
            instrument=r["instrument"], market=Market(r["market"]),
            ex_date=date.fromisoformat(r["ex_date"]),
            pay_date=date.fromisoformat(r["pay_date"]) if r["pay_date"] else None,
            cash_amount=from_db(r["cash_amount"]) if r["cash_amount"] else None,
            stock_amount=from_db(r["stock_amount"]) if r["stock_amount"] else None,
            currency=Currency(r["currency"]) if r["currency"] else None,
            source=r["source"],
        )
        for r in rows
    ]
