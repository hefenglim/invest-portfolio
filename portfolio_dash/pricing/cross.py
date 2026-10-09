"""Derived cross rates — one base currency; the third pair is COMPUTED, never fetched.

Owner ruling 2026-09-16 (demo audit M1, option (b)). The three reporting pairs used to be
fetched independently — USD/TWD, USD/MYR and MYR/TWD — and independently fetched rates do
not close a triangle. Measured on the demo with all three dated 2026-09-15 and none stale:
USD/MYR × MYR/TWD = 31.856972 against a direct USD/TWD of 31.834999, **+0.0690%** — enough
for a 4,000 MYR spot conversion to move the report-currency total by 40.88 TWD with no
ledger change at all (the conversion path differed from the valuation path). MYR/TWD has no
liquid direct market of its own: the provider's ``MYRTWD=X`` is itself a cross through USD,
sampled at a different moment, which is the whole gap. So the honest fix is to DERIVE it
here from the two pairs that DO have a direct market, and to store it with a source that
says so (``derived:USD``). After this, ``freshness.fx_triangulation`` turns from a warning
into a guard that must read 0.0000 — a non-zero gap means a row was written by something
other than this module.

Rejected: base = TWD (would derive USD/MYR, the one pair with a real market, and the pair
the Moomoo FX pool's acquisition cost is booked in); deriving at READ time in
``shared/fx`` (the stored row and the used rate would differ — two truths, and the freshness
panel would show a number the valuation never used).

Generic over :data:`CROSS_RATES`, so a fourth currency needs a table row, not code.
Pricing-internal: the two refresh paths call :func:`derive_cross_rates` right after their
``upsert_fx`` and nothing outside ``pricing/`` writes FX rows (``architecture.md``).
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel

from portfolio_dash.pricing.refs import FxPair
from portfolio_dash.pricing.results import FxRow
from portfolio_dash.pricing.store import stored_rate_text, upsert_fx
from portfolio_dash.shared.enums import Currency


class CrossRate(BaseModel, frozen=True):
    """``base/quote`` derived through ``via``: rate = rate(via/quote) ÷ rate(via/base)."""

    base: Currency
    quote: Currency
    via: Currency


# The only cross today. MYR/TWD via USD: 1 MYR = (TWD per USD) ÷ (MYR per USD) TWD.
CROSS_RATES: tuple[CrossRate, ...] = (
    CrossRate(base=Currency.MYR, quote=Currency.TWD, via=Currency.USD),
)


def derived_source(via: Currency) -> str:
    """The ``fx_rates.source`` a derived row carries — greppable, never a provider name."""
    return f"derived:{via.value}"


def cross_for(base: Currency, quote: Currency) -> CrossRate | None:
    for c in CROSS_RATES:
        if c.base is base and c.quote is quote:
            return c
    return None


def is_derived_pair(base: Currency, quote: Currency) -> bool:
    return cross_for(base, quote) is not None


def fetched_pairs(pairs: list[FxPair]) -> list[FxPair]:
    """*pairs* minus the derived ones — what a provider is actually asked for.

    A derived pair must never reach a provider: a fetched MYR/TWD row landing in the same
    run would be overwritten by the derivation a moment later, and until then two writers
    would own one row.
    """
    return [p for p in pairs if not is_derived_pair(p.base, p.quote)]


def derive_cross_rates(
    conn: sqlite3.Connection, *, fetched_at: datetime
) -> list[FxRow]:
    """Bring every derived cross-rate row into line with its legs; return every derived row.

    For each :class:`CrossRate` and EVERY ``as_of_date`` on which both legs are stored,
    ``rate = leg_quote ÷ leg_base`` in ``Decimal`` — the seam's 6-dp cap applies on the way
    in, exactly as it does to a provider rate.

    **Derived over the whole history, WRITTEN only where it differs** (CR-14, 2026-10-04). A
    day's stored row is rewritten when it is not what the seam would store for the
    derivation — compared on the stored TEXT (:func:`~pricing.store.stored_rate_text`, the
    seam's own expression) and on the source. That is exactly: a day whose leg this run wrote
    or changed; a gap (both legs, no derived row); and a row another writer left on a day
    nobody re-fetched — a provider row that predates this module (a v0.1.28 database fetched
    MYR/TWD for its whole history), a seed's fixed series, a hand edit — so one 更新報價 (or
    one 回補歷史) still re-expresses the past, not just today. A day whose value has not moved
    is not touched, so its ``fetched_at`` keeps saying when that value was set: until CR-14
    every FX write re-upserted the whole history (~1,800 rows for five years, three quote
    jobs a day) and restamped it. Why the scope is not "the dates this run touched": those
    foreign rows sit on dates no run touches. The read is one indexed join per cross; the
    writes are what cost, and they are now proportional to what changed. Idempotent: a repeat
    run writes nothing.

    Returns every derived row, written or already current, in date order — the pair's stored
    state after the call. The refresh paths report the pair from it (``RefreshSummary.ok``), so
    a run that changed no MYR/TWD value still reports the pair, as before.

    Degrades to ``[]`` on a database without ``fx_rates`` (a ledger-only bootstrap) — the
    callers run right after ``upsert_fx``, so that branch only guards a direct caller.
    """
    derived: list[FxRow] = []
    for c in CROSS_RATES:
        source = derived_source(c.via)
        try:
            rows = conn.execute(
                "SELECT q.as_of_date, q.rate, b.rate, d.rate, d.source FROM fx_rates q "
                "JOIN fx_rates b ON b.as_of_date = q.as_of_date "
                "LEFT JOIN fx_rates d ON d.as_of_date = q.as_of_date "
                "AND d.base = ? AND d.quote = ? "
                "WHERE q.base = ? AND q.quote = ? AND b.base = ? AND b.quote = ? "
                "ORDER BY q.as_of_date",
                (c.base.value, c.quote.value,
                 c.via.value, c.quote.value, c.via.value, c.base.value),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        out: list[FxRow] = []
        stale: list[FxRow] = []
        for as_of_s, leg_quote_s, leg_base_s, stored_rate, stored_source in rows:
            leg_quote = Decimal(str(leg_quote_s))
            leg_base = Decimal(str(leg_base_s))
            if leg_quote <= 0 or leg_base <= 0:
                continue  # the write seam refuses these; belt and braces for hand edits
            row = FxRow(
                base=c.base, quote=c.quote, as_of=date.fromisoformat(str(as_of_s)),
                rate=leg_quote / leg_base, source=source,
            )
            out.append(row)
            if stored_source != source or stored_rate != stored_rate_text(row.rate):
                stale.append(row)
        if stale:
            upsert_fx(conn, stale, fetched_at=fetched_at)
        derived.extend(out)
    return derived


__all__ = [
    "CROSS_RATES",
    "CrossRate",
    "cross_for",
    "derive_cross_rates",
    "derived_source",
    "fetched_pairs",
    "is_derived_pair",
]
