# mypy: ignore-errors
"""Live coverage probe for the W3 fundamentals union (AI-D13..D16).

Runs each enabled source's REAL fetch seam over a small fixed symbol set (one per
market plus a thin MY counter) and prints which canonical fields actually came back —
the probe-first evidence AI-D16 requires before the daily/weekly cadence is trusted.
Keyed legs (Finnhub, Alpha Vantage) read their key from the environment (``FINNHUB_KEY``
/ ``ALPHAVANTAGE_KEY``) and report honestly when keyless. Prints only: writes nothing to
any DB and nothing into the repo.

Usage::

    .venv/Scripts/python.exe scripts/probe_fundamentals.py [SYMBOL ...]

With no arguments it probes the default set (US large-cap, TW listed, MY board-lot, MY
sub-RM1). Extra arguments are bare symbols in the markets above (``AAPL``, ``2330``,
``1155`` — market is inferred by digits vs letters).
"""

import os
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from portfolio_dash.pricing import fundamentals_source as F  # noqa: E402
from portfolio_dash.pricing.refs import InstrumentRef  # noqa: E402
from portfolio_dash.shared.enums import Market  # noqa: E402

_DEFAULT_REFS = [
    InstrumentRef(symbol="AAPL", market=Market.US),
    InstrumentRef(symbol="2330", market=Market.TW, board="TWSE"),
    InstrumentRef(symbol="1155", market=Market.MY),   # Maybank (board lot)
    InstrumentRef(symbol="0097", market=Market.MY),   # VSTECS (sub-RM1, thin coverage check)
]


def _ref_for(symbol: str) -> InstrumentRef:
    if symbol.isdigit() and len(symbol) == 4:
        return InstrumentRef(symbol=symbol, market=Market.TW, board="TWSE")
    if symbol.isdigit():
        return InstrumentRef(symbol=symbol, market=Market.MY)
    return InstrumentRef(symbol=symbol, market=Market.US)


def main() -> int:
    refs = [_ref_for(s) for s in sys.argv[1:]] or _DEFAULT_REFS
    as_of = date.today()
    keys = {
        "yfinance": "(key-less)",
        "finnhub": "set" if os.environ.get("FINNHUB_KEY") else "MISSING",
        "alphavantage": "set" if os.environ.get("ALPHAVANTAGE_KEY") else "MISSING",
    }
    print(f"as_of={as_of}  keys: " + ", ".join(f"{k}={v}" for k, v in keys.items()))
    cols = [f"{s[:3]}/{r.symbol}" for s in F.SOURCES for r in refs]
    rows: dict[str, list[str]] = {
        field: [] for field in (*F.CANONICAL_FIELDS, "(meta) currency", "(block written)")
    }
    for source in F.SOURCES:
        fetch = F.FETCHERS[source]
        token = os.environ.get("FINNHUB_KEY") if source == "finnhub" else (
            os.environ.get("ALPHAVANTAGE_KEY") if source == "alphavantage" else None
        )
        for ref in refs:
            block = fetch(ref, as_of=as_of, token=token)
            for field in F.CANONICAL_FIELDS:
                rows[field].append(str((block or {}).get(field, "-")))
            rows["(meta) currency"].append(str((block or {}).get("currency", "-")))
            rows["(block written)"].append("yes" if block else "NO")
    label_w = max(len(f) for f in rows)
    widths = [max(len(c), 14) for c in cols]  # 14: a full market_cap never truncates
    print(" " * (label_w + 1) + " | ".join(c.ljust(w) for c, w in zip(cols, widths, strict=True)))
    for field, values in rows.items():
        print(
            field.ljust(label_w) + " "
            + " | ".join(v[: w].ljust(w) for v, w in zip(values, widths, strict=True))
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
