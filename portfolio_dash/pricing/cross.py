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
from portfolio_dash.pricing.store import upsert_fx
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
    """Write every derivable cross-rate row and return what was written.

    For each :class:`CrossRate` and EVERY ``as_of_date`` on which both legs are stored,
    ``rate = leg_quote ÷ leg_base`` in ``Decimal`` — the seam's 6-dp cap applies on the way
    in, exactly as it does to a provider rate. Whole-history on purpose: a provider row for
    the derived pair that predates this module is replaced the first time either leg is
    refreshed, so one 更新報價 (or one 回補歷史) re-expresses the past, not just today. The
    table is three pairs by a few years of days; the join is cheap.

    Degrades to ``[]`` on a database without ``fx_rates`` (a ledger-only bootstrap) — the
    callers run right after ``upsert_fx``, so that branch only guards a direct caller.
    """
    written: list[FxRow] = []
    for c in CROSS_RATES:
        try:
            rows = conn.execute(
                "SELECT q.as_of_date, q.rate, b.rate FROM fx_rates q "
                "JOIN fx_rates b ON b.as_of_date = q.as_of_date "
                "WHERE q.base = ? AND q.quote = ? AND b.base = ? AND b.quote = ?",
                (c.via.value, c.quote.value, c.via.value, c.base.value),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        out: list[FxRow] = []
        for as_of_s, leg_quote_s, leg_base_s in rows:
            leg_quote = Decimal(str(leg_quote_s))
            leg_base = Decimal(str(leg_base_s))
            if leg_quote <= 0 or leg_base <= 0:
                continue  # the write seam refuses these; belt and braces for hand edits
            out.append(FxRow(
                base=c.base, quote=c.quote, as_of=date.fromisoformat(str(as_of_s)),
                rate=leg_quote / leg_base, source=derived_source(c.via),
            ))
        if out:
            upsert_fx(conn, out, fetched_at=fetched_at)
            written.extend(out)
    return written


__all__ = [
    "CROSS_RATES",
    "CrossRate",
    "cross_for",
    "derive_cross_rates",
    "derived_source",
    "fetched_pairs",
    "is_derived_pair",
]
