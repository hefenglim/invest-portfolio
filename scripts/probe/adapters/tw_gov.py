# mypy: ignore-errors
"""TW government open data: TWSE (上市) and TPEx (上櫃)."""

import requests

TWSE_DAY = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
TPEX_DAILY = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"

LISTED_TW = ["0050", "2454", "2330", "2543", "2317", "3005", "2308", "1519"]
OTC_TWO = ["8299", "6488", "6531", "6139"]


def fetch_twse_day(stock_no: str, yyyymmdd: str) -> dict:
    resp = requests.get(
        TWSE_DAY, params={"response": "json", "date": yyyymmdd, "stockNo": stock_no},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def parse_twse_close(payload: dict) -> str | None:
    """Last row's close from a STOCK_DAY payload (close is the 7th column)."""
    if payload.get("stat") != "OK" or not payload.get("data"):
        return None
    return payload["data"][-1][6]


def fetch_tpex_daily() -> list[dict]:
    resp = requests.get(TPEX_DAILY, timeout=15)
    resp.raise_for_status()
    return resp.json()


def tpex_close_for(rows: list[dict], code: str) -> str | None:
    for row in rows:
        if row.get("SecuritiesCompanyCode") == code:
            return row.get("Close")
    return None
