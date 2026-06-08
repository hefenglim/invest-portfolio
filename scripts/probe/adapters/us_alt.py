# mypy: ignore-errors
"""US alternative sources: stockprices.dev (no key), AlphaVantage + Finnhub (keyed).

Discovery note (probe time): stockprices.dev advertises itself as "a minimal, free
JSON API for fetching real-time stock prices. No auth, no limits, built with Go."
Its homepage (https://stockprices.dev/) documents two endpoints:

    GET https://stockprices.dev/api/stocks/:ticker   - for companies (e.g. AAPL, NVDA)
    GET https://stockprices.dev/api/etfs/:ticker     - for ETFs (e.g. VOO, QQQ)

A live GET to ``/api/stocks/AAPL`` returned (HTTP 200, JSON):

    {"Ticker":"AAPL","Name":"Apple Inc.","Price":307.34,
     "ChangeAmount":-3.89,"ChangePercentage":-1.25}

So the close/last price lives under the **capitalized** key ``"Price"`` (a JSON
number, not a string) — not ``price``/``close`` as the original stub guessed. The
endpoint also appears to rate-limit aggressively despite "no limits" marketing
(a follow-up call to ``/api/etfs/VOO`` returned ``429 Too Many Requests``), so
treat it as a fallback source, not a primary one, and expect occasional throttling.
"""

import os

import requests

ALPHA = "https://www.alphavantage.co/query"
FINNHUB = "https://finnhub.io/api/v1/quote"
STOCKPRICES = "https://stockprices.dev/api/stocks"


def alpha_key() -> str | None:
    return os.environ.get("ALPHAVANTAGE_KEY")


def finnhub_key() -> str | None:
    return os.environ.get("FINNHUB_KEY")


def fetch_stockprices(symbol: str) -> dict:
    # Discovered endpoint: GET https://stockprices.dev/api/stocks/{ticker}
    # (use /api/etfs/{ticker} instead for ETF symbols).
    resp = requests.get(f"{STOCKPRICES}/{symbol}", timeout=15)
    resp.raise_for_status()
    return resp.json()


def parse_stockprices_close(payload: dict):
    # Real response shape uses capitalized keys, e.g.
    # {"Ticker": "AAPL", "Name": "Apple Inc.", "Price": 307.34, ...}
    return payload.get("Price")


def fetch_alpha_global_quote(symbol: str, key: str) -> dict:
    resp = requests.get(
        ALPHA, params={"function": "GLOBAL_QUOTE", "symbol": symbol, "apikey": key},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def parse_alpha_close(payload: dict) -> str | None:
    return (payload.get("Global Quote") or {}).get("05. price")


def fetch_finnhub_quote(symbol: str, key: str) -> dict:
    resp = requests.get(FINNHUB, params={"symbol": symbol, "token": key}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def parse_finnhub_close(payload: dict) -> float | None:
    return payload.get("c")  # Finnhub quote: c=current, pc=previous close
