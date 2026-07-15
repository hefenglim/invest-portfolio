"""Trade-date FX lookup for cross-currency fee computation (Moomoo US MY stamp, FE-D2).

``fees.compute_fees`` is PURE and takes the USD/MYR rate as a parameter. The caller seams
(manual entry, CSV import, ledger edit-recompute, rebalance/what-if estimates) resolve the
latest stored USD/MYR rate on-or-before the trade date here and pass it in — exactly as they
resolve ``is_etf`` from the instrument registry.

The read is a direct SQL SELECT on the ``fx_rates`` table (owned by ``pricing/``): no
``pricing`` import, mirroring the established sibling-decoupling pattern (``pricing/ingest``
reads sibling tables the same way). This keeps ``data_ingestion`` free of a pricing import
while every higher-layer caller (api, strategy) can reuse the one helper.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.money import from_db

_ONE = Decimal("1")


def _rate_on(conn: sqlite3.Connection, base: str, quote: str, on: date) -> Decimal | None:
    row = conn.execute(
        "SELECT rate FROM fx_rates WHERE base=? AND quote=? AND as_of_date<=? "
        "ORDER BY as_of_date DESC LIMIT 1",
        (base, quote, on.isoformat()),
    ).fetchone()
    return from_db(row[0]) if row is not None else None


def resolve_stamp_fx(conn: sqlite3.Connection, on: date) -> Decimal | None:
    """Latest stored USD→MYR rate with ``as_of_date <= on`` (direct, then inverse), else None.

    Used for the Moomoo US MY stamp duty (FE-D2). ``None`` means the caller books the stamp
    as 0 and surfaces the soft issue 「無 USD/MYR 匯率,印花稅未計」 (preview paths) or a
    snapshot note (estimate paths).
    """
    direct = _rate_on(conn, Currency.USD.value, Currency.MYR.value, on)
    if direct is not None:
        return direct
    inverse = _rate_on(conn, Currency.MYR.value, Currency.USD.value, on)
    if inverse is not None and inverse > 0:
        return _ONE / inverse
    return None
