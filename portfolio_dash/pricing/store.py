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
from typing import Protocol

from portfolio_dash.pricing.results import DividendEvent, FxRead, FxRow, PriceRead, PriceRow
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.money import cap_dp, from_db, to_db

_DEFAULT_MAX_AGE = 4  # days
_ONE = Decimal(1)
# The canonical TEXT for "no factor applied" — the ``split_basis`` DDL default, so a row
# written by this seam for a symbol with no corporate action is indistinguishable from a
# legacy row the migration defaulted. Materialised once so the two can never diverge.
_IDENTITY_BASIS = "1"

# Float-noise caps (2026-07-03, human sign-off): float-sourced providers (yfinance
# et al.) emit binary-float tails ("305.364990234375") that are NOT source
# precision. Prices cap at 4 dp (covers every market tick: US/TW 2 dp, MY 3 dp);
# FX rates cap at 6 dp (rates are not money; rules allow 4-6 dp). CAP, never pad:
# values already at or under the cap store byte-identical.
_PRICE_DP = 4
_FX_DP = 6


def _cap_dp(v: Decimal, places: int) -> Decimal:
    """Round ``v`` to at most ``places`` decimals; values within the cap unchanged."""
    return cap_dp(v, places)


def _opt(v: Decimal | None) -> str | None:
    return to_db(_cap_dp(v, _PRICE_DP)) if v is not None else None


def _opt_raw(v: Decimal | None) -> str | None:
    """A ``*_raw`` column: the provider's value verbatim, NOT capped.

    The cap belongs on the DERIVED value, applied last and to the product. A raw column is
    the input the reconcile recomputes from, so capping it here would make the first
    reconcile silently move the derived value onto a slightly different number — the exact
    reason ``close_raw`` has always been written through ``to_db`` rather than ``_opt``.
    """
    return to_db(v) if v is not None else None


class SplitFactorFn(Protocol):
    """The split factor already folded into a fetched price row, injected (D17).

    ``pricing/`` may not import ``data_ingestion`` (``architecture.md``), and the ratio
    lookup needs the corporate-action ledger — so the lookup arrives as a callable that
    the ``api`` / ``scheduler`` layer binds. ``pricing/`` therefore never learns that
    corporate actions exist; it only knows a number multiplies its raw close.

    **Keyword-only dates, deliberately (F-22).** The window is over TWO dates
    (``after < action.date <= through``), which the spec's earlier
    ``Callable[[str, date], Decimal]`` could not express. Naming both at every call site
    makes the window impossible to misread or to transpose, and mirrors
    ``shared.corporate_actions.split_factor``'s own signature keyword for keyword.
    Binding ``through`` in a closure instead was rejected: the caller passes
    ``fetched_at`` to :func:`upsert_prices` separately, so a closure could silently
    capture a different timestamp than the one written to the row, and nothing would
    detect the mismatch. Here the row's own ``fetched_at`` IS the upper bound, by
    construction.
    """

    def __call__(self, symbol: str, *, after: date, through: date) -> Decimal: ...


def _no_factor(symbol: str, *, after: date, through: date) -> Decimal:
    """The identity factor — no corporate-action ledger injected, so nothing is applied.

    The default, so every existing caller and test is unchanged and a ledger with no
    corporate actions stores byte-identically (``Decimal(1)`` serializes to ``"1"``,
    which is also the column's DDL default).
    """
    return _ONE


def express_close(raw: Decimal, basis: Decimal) -> tuple[str, str]:
    """``(close, split_basis)`` as stored TEXT, for a provider close under a split factor.

    **THE one owner of the price-basis expression** (§6.0, "ONE owner per concept").
    §5.1(c) defines two operations — the write seam on every fetch and W6b's reconcile on
    every SPLIT insert/edit/delete — and defines them as the *same* restatement,
    ``close := raw × target``. Two copies of that expression would be two things to keep
    in step, and their disagreement is invisible to a value assertion: both would store
    the same *number* and differ only in the stored TEXT, which is precisely what D38
    invariant 3 forbids.

    Two properties, both load-bearing and both measured:

    * **The cap goes LAST, on the product** (F-20). ``_cap_dp(raw, 4) × basis`` amplifies
      the cap's own error by the factor: the float tail ``0.14166666865348816`` at ``×20``
      stores 2.8340 that way and 2.8333 correctly, and at ``×3`` it is 0.4251 vs 0.4250 —
      the sub-RM1 MY tick ``data-and-pricing.md`` singles out.
    * **The identity takes the untouched pre-existing expression**, structurally, rather
      than multiplying by one and trusting the answer (owner requirement 2026-08-10).
      ``Decimal`` multiplication sums the operands' EXPONENTS, so a factor of
      ``Decimal("1.0")`` instead of ``Decimal(1)`` rewrites ``1.5`` as ``1.50``, ``600`` as
      ``600.0`` and ``0.005`` as ``0.0050`` — value-preserving, TEXT-changing, and
      :func:`_cap_dp` does not catch it (the cap fires only BELOW 4 dp and never trims).
      Since Decimals persist as canonical TEXT, computing the identity would repaint every
      price row in the database, on symbols with no corporate action at all.
      ``Decimal("1.0") == Decimal(1)`` is ``True``, so such a factor routes to the safe
      path too, and the basis is stored as the literal :data:`_IDENTITY_BASIS`.
    """
    if basis == _ONE:
        return to_db(_cap_dp(raw, _PRICE_DP)), _IDENTITY_BASIS
    return to_db(_cap_dp(raw * basis, _PRICE_DP)), to_db(basis)


def express_optional(raw: Decimal | None, basis: Decimal) -> str | None:
    """One OHLC column OTHER than the close, under the row's factor — or ``None``.

    Delegates to :func:`express_close` rather than repeating the expression, so the price
    basis keeps exactly ONE owner (§6.0) and the identity short-circuit, the cap-goes-last
    rule and the no-repaint guarantee apply to all four columns by construction instead of
    by four copies staying in step.

    ``None`` in, ``None`` out: providers routinely omit OHLC, and a factor must never
    conjure a value out of a missing one.
    """
    if raw is None:
        return None
    value, _ = express_close(raw, basis)
    return value


def upsert_prices(
    conn: sqlite3.Connection,
    rows: list[PriceRow],
    *,
    fetched_at: datetime,
    factor_of: SplitFactorFn = _no_factor,
) -> None:
    """Upsert quote rows into ``prices``, keyed on (instrument, as_of_date).

    OHLC values are float-noise-capped to 4 dp on the way in (the ONLY price
    write seam, so every provider is covered).

    **The price basis (spec §5.1(b)/(c), D30).** A provider re-states its history after
    a split, so the close it delivers for a pre-split date is expressed in post-split
    share terms while the ledger's share count for that date is not. ``factor_of``
    returns the splits the provider had already folded in when this row was fetched —
    the window ``(row.as_of, fetched_at]`` — and the row is stored as:

    * ``close_raw``  = the provider's value **exactly as delivered**, un-capped;
    * ``split_basis`` = the factor applied;
    * ``close``      = ``close_raw × split_basis``, capped to 4 dp.

    Three consequences worth stating, because each one is a bug that was measured:

    1. **The cap goes LAST, on the product** (F-20). ``_cap_dp(raw, 4) × target``
       amplifies the cap's error by the factor: ``0.14166666865348816 × 20`` stores
       2.8340 that way and 2.8333 correctly, and at ``× 3`` it is 0.4251 vs 0.4250 —
       the sub-RM1 MY case ``data-and-pricing.md`` singles out.
    2. **``close_raw`` is stored UN-capped**, unlike every other price column. The
       reconcile (W6b) recomputes ``close := close_raw × target`` from this stored
       value, so it must be the same input the write used — capping it here would make
       the first reconcile silently move every price back onto the (1) value.
       ``data-and-pricing.md`` asks for "full source precision" and says the cap
       "removes representation noise, not information"; once the close is derived, the
       source it derives from is the thing that must not lose information.
    3. ``open`` / ``high`` / ``low`` are expressed **exactly like the close** (W8, owner
       ruling 2026-08-27) — each has its own ``*_raw``, so the derived value is always
       restatable and reversible, which is the condition the earlier design said had to
       be met first. Until W8 they kept the provider's basis, so a row could carry a
       post-split close beside pre-split highs and lows; harmless only because nothing
       read them, and a trap for the first candlestick chart. ``volume`` is STILL left
       untouched and deliberately so: it is a count, not a price, and a split restates it
       in the opposite direction — giving it this factor would be wrong, not merely
       unrestatable.

    **No factor → the pre-existing code path, structurally** (owner requirement,
    2026-08-10), and **the cap applied last, to the product** (F-20). Both live in
    :func:`express_close`, which W6b's reconcile also calls — see its docstring for the
    measured traps each one closes. This seam owns only *which* factor applies to a row
    (the window ``(as_of, fetched_at]``); how a (raw, factor) pair becomes stored TEXT has
    exactly one owner, so the write and the reconcile cannot drift apart.
    """
    through = fetched_at.date()
    params: list[tuple[str | None, ...]] = []
    for r in rows:
        basis = factor_of(r.instrument, after=r.as_of, through=through)
        close, stored_basis = express_close(r.close, basis)
        params.append((
            r.instrument, r.market.value, r.as_of.isoformat(), close,
            express_optional(r.open, basis), express_optional(r.high, basis),
            express_optional(r.low, basis), _opt(r.volume), r.source,
            fetched_at.isoformat(), to_db(r.close),
            _opt_raw(r.open), _opt_raw(r.high), _opt_raw(r.low), stored_basis,
        ))
    conn.executemany(
        # F-23: the basis columns MUST be restated by DO UPDATE. Left out, a re-fetch
        # writes a new close over a stale basis, and the next reconcile compounds the
        # error from the wrong starting point — silently, and only on a re-fetch.
        """INSERT INTO prices (instrument, market, as_of_date, close, open, high, low,
               volume, source, fetched_at, close_raw, open_raw, high_raw, low_raw,
               split_basis)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(instrument, as_of_date) DO UPDATE SET
               close=excluded.close, open=excluded.open, high=excluded.high, low=excluded.low,
               volume=excluded.volume, source=excluded.source, fetched_at=excluded.fetched_at,
               close_raw=excluded.close_raw, open_raw=excluded.open_raw,
               high_raw=excluded.high_raw, low_raw=excluded.low_raw,
               split_basis=excluded.split_basis""",
        params,
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
        "SELECT close, as_of_date, source, volume, fetched_at FROM prices WHERE instrument=? "
        "AND as_of_date BETWEEN ? AND ? ORDER BY as_of_date ASC",
        (instrument, start.isoformat(), end.isoformat()),
    ).fetchall()
    return [
        PriceRead(value=from_db(r["close"]), as_of=date.fromisoformat(r["as_of_date"]),
                  source=r["source"], stale=False,
                  volume=from_db(r["volume"]) if r["volume"] is not None else None,
                  fetched_at=(datetime.fromisoformat(r["fetched_at"])
                              if r["fetched_at"] is not None else None))
        for r in rows
    ]


def price_dates(conn: sqlite3.Connection, instrument: str) -> list[date]:
    """All stored ``as_of_date``s for ``instrument``, ascending — date-only read.

    The signal-history replay (W6) needs the SET of trading dates (to diff against the
    stored history), not the closes; parsing every ``PriceRead`` for that would waste the
    Decimal conversions. The table's PK already serves this scan.
    """
    rows = conn.execute(
        "SELECT as_of_date FROM prices WHERE instrument=? ORDER BY as_of_date ASC",
        (instrument,),
    ).fetchall()
    return [date.fromisoformat(r["as_of_date"]) for r in rows]


def upsert_fx(conn: sqlite3.Connection, rows: list[FxRow], *, fetched_at: datetime) -> None:
    """Upsert FX rate rows into ``fx_rates``, keyed on (base, quote, as_of_date).

    Rates are float-noise-capped to 6 dp on the way in (rates are not money;
    the 4-6 dp high-precision rule in data-and-pricing.md still holds).
    """
    conn.executemany(
        """INSERT INTO fx_rates (base, quote, as_of_date, rate, source, fetched_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(base, quote, as_of_date) DO UPDATE SET
               rate=excluded.rate, source=excluded.source, fetched_at=excluded.fetched_at""",
        [(r.base.value, r.quote.value, r.as_of.isoformat(), to_db(_cap_dp(r.rate, _FX_DP)),
          r.source, fetched_at.isoformat()) for r in rows],
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


def get_fx_on(
    conn: sqlite3.Connection, base: Currency, quote: Currency, *, on: date,
) -> FxRead | None:
    """Return the most recent stored rate with ``as_of_date <= on``, or ``None``.

    Point-in-time read for trade-date conversion: never a later rate ("never guess
    backwards"). ``stale`` is always False — staleness is a latest-quote concern
    (same convention as ``get_price_history``).
    """
    row = conn.execute(
        "SELECT rate, as_of_date, source FROM fx_rates WHERE base=? AND quote=? "
        "AND as_of_date<=? ORDER BY as_of_date DESC LIMIT 1",
        (base.value, quote.value, on.isoformat()),
    ).fetchone()
    if row is None:
        return None
    return FxRead(rate=from_db(row["rate"]), as_of=date.fromisoformat(row["as_of_date"]),
                  source=row["source"], stale=False)


def get_fx_history(
    conn: sqlite3.Connection, base: Currency, quote: Currency, start: date, end: date,
) -> list[FxRead]:
    """Return stored FX rates for ``base``/``quote`` within ``[start, end]``, ascending."""
    rows = conn.execute(
        "SELECT rate, as_of_date, source FROM fx_rates WHERE base=? AND quote=? "
        "AND as_of_date BETWEEN ? AND ? ORDER BY as_of_date ASC",
        (base.value, quote.value, start.isoformat(), end.isoformat()),
    ).fetchall()
    return [
        FxRead(rate=from_db(r["rate"]), as_of=date.fromisoformat(r["as_of_date"]),
               source=r["source"], stale=False)
        for r in rows
    ]


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
