"""VIX + CNN Fear & Greed sentiment client (spec 20.7).

Free, key-less market-sentiment signals for the external-snapshot ingest:

* **VIX** — yfinance ``^VIX`` last close.
* **Fear & Greed** — CNN's public graphdata JSON (score 0-100 + rating).

Both are *decision-support signals*, not numbers of record. Every numeric value is
parsed through ``Decimal(str(x))`` (no float into the money chain). Any failure
degrades to ``None`` so a missing source never crashes ingest or fabricates a value.
All external I/O is isolated in the two private ``_vix_last_close`` / ``_cnn_graphdata``
seams so tests monkeypatch them (the repo bans sockets in tests).
"""

from decimal import Decimal, InvalidOperation
from typing import Any

import requests
import yfinance as yf

_CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
_CNN_TIMEOUT_S = 10
# CNN's endpoint 403s a bare client; a desktop UA is required.
_CNN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}


def _to_decimal(value: object) -> Decimal | None:
    """Decimal(str(value)) if finite, else None (filters NaN/inf/None/garbage)."""
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _vix_last_close() -> float | None:
    """The ^VIX last available close from yfinance (None when empty)."""
    df = yf.Ticker("^VIX").history(period="5d", auto_adjust=False)
    if df is None or df.empty:
        return None
    for _, close in reversed(list(df["Close"].items())):
        if close is not None:
            return float(close)
    return None


def _cnn_graphdata() -> dict[str, Any]:
    """Raw CNN Fear & Greed graphdata JSON."""
    resp = requests.get(_CNN_URL, headers=_CNN_HEADERS, timeout=_CNN_TIMEOUT_S)
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    return data


def fetch_vix() -> Decimal | None:
    """Latest VIX close as Decimal, or None on any failure (graceful degradation)."""
    try:
        return _to_decimal(_vix_last_close())
    except Exception:  # noqa: BLE001 - any source failure degrades to None
        return None


def fetch_fear_greed() -> dict[str, Any] | None:
    """CNN Fear & Greed as ``{"score": Decimal, "rating": str}``, or None on failure."""
    try:
        payload = _cnn_graphdata()
        block = payload.get("fear_and_greed")
        if not isinstance(block, dict):
            return None
        score = _to_decimal(block.get("score"))
        rating = block.get("rating")
        if score is None or not isinstance(rating, str):
            return None
        return {"score": score, "rating": rating}
    except Exception:  # noqa: BLE001 - any source failure degrades to None
        return None
