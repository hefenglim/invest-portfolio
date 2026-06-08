# mypy: ignore-errors
"""yfinance adapter: latest/history quotes, FX, dividends across US/TW/MY."""

from decimal import Decimal

import yfinance as yf

US = ["TSLA", "AAPL", "NVDA", "IVV", "VOO", "RIVN", "O", "BEN", "BABA",
      "GOOGL", "MSFT", "MU", "SNDK", "ARKK", "GGR", "SE"]
TW = ["0050", "8299", "2454", "2330", "6488", "6531", "2543", "2317",
      "3005", "6139", "2308", "1519"]
MY = ["5212", "3182", "5347", "1155", "1818"]
FX = ["USDTWD=X", "USDMYR=X", "MYRTWD=X"]


def fetch_history_df(symbol: str, period: str = "5y"):
    return yf.Ticker(symbol).history(period=period, auto_adjust=False)


def parse_latest_close(df) -> Decimal | None:
    """Last raw Close from a yfinance history DataFrame (None if empty)."""
    if df is None or df.empty or "Close" not in df.columns:
        return None
    return Decimal(str(df["Close"].iloc[-1]))


def has_raw_and_adj(df) -> bool:
    return df is not None and {"Close", "Adj Close"}.issubset(df.columns)


def max_decimals(df) -> int:
    """Max decimal places seen in the Close column (for MY 3-dp fidelity)."""
    if df is None or df.empty:
        return 0
    return max(
        (len(str(v).split(".")[-1]) if "." in str(v) else 0) for v in df["Close"]
    )
