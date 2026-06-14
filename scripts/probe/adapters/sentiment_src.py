# mypy: ignore-errors
"""Sentiment + index probe sources (spec 20.7/20.11): VIX, CNN Fear & Greed, indices.

All key-less. VIX and the three benchmark indices come from yfinance (``^VIX`` /
``^TWII`` / ``^GSPC`` / ``^KLSE``); Fear & Greed comes from CNN's public graphdata
JSON (a desktop UA is required or it 403s). These adapters expose pure parsers (unit-
tested without network) plus thin live fetchers used by ``run_all``.
"""

import requests

CNN_FNG = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
INDEX_SYMBOLS = ["^TWII", "^GSPC", "^KLSE"]
VIX_SYMBOL = "^VIX"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}


def fetch_cnn_fng() -> dict:
    resp = requests.get(CNN_FNG, headers=_HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json()


def parse_fng(payload: dict) -> dict | None:
    """Extract ``{"score", "rating"}`` from CNN graphdata, or None when malformed."""
    block = payload.get("fear_and_greed")
    if not isinstance(block, dict):
        return None
    score = block.get("score")
    rating = block.get("rating")
    if score is None or not isinstance(rating, str):
        return None
    return {"score": score, "rating": rating}


def fetch_yf_close(symbol: str) -> float | None:
    """Last available close for a yfinance symbol (VIX / index), or None when empty."""
    import yfinance as yf

    df = yf.Ticker(symbol).history(period="5d", auto_adjust=False)
    if df is None or df.empty:
        return None
    for _, close in reversed(list(df["Close"].items())):
        if close is not None:
            return float(close)
    return None
