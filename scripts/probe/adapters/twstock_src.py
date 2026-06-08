# mypy: ignore-errors
"""twstock TW intraday + history."""

import twstock


def fetch_twstock_realtime(code: str) -> dict:
    return twstock.realtime.get(code)


def parse_twstock_price(payload: dict) -> str | None:
    if not payload.get("success"):
        return None
    return payload["realtime"]["latest_trade_price"]
