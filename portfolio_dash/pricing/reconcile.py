"""Restate stored closes when the SPLIT ledger changes (spec §5.1(c), D30 · W6b).

``upsert_prices`` establishes the invariant on every fetch; this module restores it when
the *other* input moves — the corporate-action ledger. Both are the SAME restatement:

    close := close_raw × target        split_basis := target

    target(row) := Π{ ratio(k) : k ∈ keys(row.instrument, row.as_of_date, row.fetched_at) }

recomputed from the stored raw provider value. **Never from the current close, and never
by dividing the old basis back out** — D30 rejected that reconstruction with measured
numbers: ``_cap_dp`` re-applies on every pass, so two splits entered in opposite orders
stored 0.0014 and 0.0015 and *neither* equalled the one-shot figure, and an already-rounded
feed could not be un-rounded at all (a US 7-for-1 reconstructed 100.07 as 100.10). Rebuilt
from the raw column, idempotency, reversibility and order-independence hold **by
construction** rather than by rounding luck, and ``prices`` rejoins the discipline the
ledgers already obey: nothing authoritative is overwritten, every derived figure is
recomputed on read (CLAUDE.md #7).

Reversibility is not a nicety here. ``prices`` is the only place this feature writes
outside the ledgers, so 重算 does not cover it — D38 invariant 3 requires that deleting a
SPLIT return every affected row **byte-identical**, which is asserted on the stored TEXT
because ``Decimal("1.5") == Decimal("1.50")`` is ``True``.

**SPLIT only** (D22 / trap #16). The scope lives in :func:`shared.corporate_actions
.split_factor`, which this module reaches only through the injected callable — an EXCHANGE
*adds to* its destination rather than re-denominating it, so applying a factor there would
corrupt the entire price history of a symbol you already held before a merger into it.

Layering: this module imports ``pricing.store`` and the standard library, nothing else
(D17 / trap #12). ``pricing/`` may not import anything above ``shared/``, so the ratio
lookup arrives as a callable that ``api``/``scheduler`` binds; ``tests/pricing/
test_layering.py`` greps every file under ``pricing/`` for both violations.
"""

import sqlite3
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal

from portfolio_dash.pricing.store import (
    SplitFactorFn,
    express_close,
    express_optional,
)
from portfolio_dash.shared.money import from_db

# ``close_raw`` is guaranteed present by ``pricing/schema.create_tables`` — the write seam
# always writes it and the migration backfills legacy rows (``close_raw = close``, which is
# exactly right: at migration the action ledger is empty, so those rows' basis is 1) — and
# ``fetched_at`` is ``NOT NULL`` in the DDL. The filter is therefore provably a no-op; it is
# here so that a hand-edited row degrades to "left untouched" instead of raising and
# stopping the restatement of every OTHER row in the same pass.
_ROWS_SQL = (
    "SELECT as_of_date, fetched_at, close, close_raw, open, open_raw, high, high_raw, "
    "low, low_raw, split_basis FROM prices "
    "WHERE instrument=? AND close_raw IS NOT NULL AND fetched_at IS NOT NULL "
    "ORDER BY as_of_date"
)

#: The optional OHLC columns, paired with their raw (W8). ``close`` is handled separately
#: because it is NOT NULL and carries the basis comparison.
_OPTIONAL_OHLC: tuple[str, ...] = ("open", "high", "low")


def _raw_or_none(text: str | None) -> Decimal | None:
    """A ``*_raw`` cell that may legitimately be absent (the provider omitted the column).

    ``from_db`` deliberately raises on anything unparseable rather than coercing, so the
    absent case is handled HERE instead of loosening that guarantee for every caller.
    """
    return None if text is None else from_db(text)


def reconcile_prices(
    conn: sqlite3.Connection, symbols: Iterable[str], *, factor_of: SplitFactorFn
) -> int:
    """Rebuild every stored close of *symbols* from its raw value. Returns rows changed.

    Call on **any insert / edit / delete of a SPLIT row** (§5.1(c)); the application-level
    entry point that binds the ledger is ``api.instrument_service.reconcile_price_basis``.
    Needs no network and works for a delisted symbol, because the input it rebuilds from is
    already stored.

    ``factor_of`` is **required, never defaulted** (deliberate). A default of
    ``_no_factor`` would type-check, read harmlessly, and silently restate every close back
    onto its un-factored value — i.e. quietly undo every split correction in the database.
    The write seam can safely default to the identity because "no ledger injected" there
    means "store what the provider sent"; here it would mean "forget what the ledger said".

    Writes only rows whose ``(close, split_basis)`` actually changes. That is an
    optimisation, **not** the idempotency mechanism (§5.1 detail 1, demoted by D30): a
    second pass recomputes the same product from the same unchanged raw value, so it is
    byte-identical whether or not it compares first. Comparing the OUTPUT rather than
    ``target == split_basis`` also repairs a row whose close has drifted from its own raw
    value, which the basis comparison alone cannot see.
    """
    updates: list[tuple[str, str, str, str]] = []
    for symbol in sorted(set(symbols)):
        for r in conn.execute(_ROWS_SQL, (symbol,)).fetchall():
            basis = factor_of(
                symbol,
                after=date.fromisoformat(r["as_of_date"]),
                through=datetime.fromisoformat(r["fetched_at"]).date(),
            )
            close, stored_basis = express_close(from_db(r["close_raw"]), basis)
            # W8: every OHLC column is restated from its OWN raw under the SAME factor.
            # Recomputed, never rescaled in place and never divided back out — so a SPLIT
            # that is inserted and then deleted restores all four byte-identically, which
            # matters because `prices` is the only place this feature writes outside the
            # ledgers and 重算 does not cover it.
            wanted = [close] + [
                express_optional(_raw_or_none(r[f"{c}_raw"]), basis) for c in _OPTIONAL_OHLC
            ]
            current = [r["close"]] + [r[c] for c in _OPTIONAL_OHLC]
            if (wanted, stored_basis) != (current, r["split_basis"]):
                updates.append((*wanted, stored_basis, symbol, r["as_of_date"]))
    if updates:
        conn.executemany(
            "UPDATE prices SET close=?, open=?, high=?, low=?, split_basis=? "
            "WHERE instrument=? AND as_of_date=?",
            updates,
        )
        conn.commit()
    return len(updates)
