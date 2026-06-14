"""yfinance index-quotes client (spec 20.7).

Fetches the three benchmark index closes used by the ``index_quotes`` sentiment
variable: TAIEX (``^TWII``), S&P 500 (``^GSPC``), KLCI (``^KLSE``). Closes are
returned as :class:`~decimal.Decimal` (``Decimal(str(x))`` — no float into the
chain); a symbol with no data or a per-symbol error is omitted (graceful
degradation). The per-symbol getter is isolated for monkeypatching (no network in
tests).
"""

from decimal import Decimal, InvalidOperation

import yfinance as yf

INDEX_SYMBOLS: tuple[str, ...] = ("^TWII", "^GSPC", "^KLSE")


def _to_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _index_last_close(symbol: str) -> float | None:
    """The last available close for one index symbol from yfinance (None when empty)."""
    df = yf.Ticker(symbol).history(period="5d", auto_adjust=False)
    if df is None or df.empty:
        return None
    for _, close in reversed(list(df["Close"].items())):
        if close is not None:
            return float(close)
    return None


def fetch_indices() -> dict[str, Decimal]:
    """Latest close per index symbol as Decimal; missing/failed symbols omitted."""
    out: dict[str, Decimal] = {}
    for symbol in INDEX_SYMBOLS:
        try:
            close = _to_decimal(_index_last_close(symbol))
        except Exception:  # noqa: BLE001 - one bad symbol must not drop the rest
            continue
        if close is not None:
            out[symbol] = close
    return out
