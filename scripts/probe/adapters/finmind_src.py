# mypy: ignore-errors
"""FinMind (keyed) TW price/fx/dividend."""

import os

import requests

FINMIND = "https://api.finmindtrade.com/api/v4/data"


def finmind_token() -> str | None:
    return os.environ.get("FINMIND_TOKEN")


def fetch_finmind(dataset: str, data_id: str, start: str, token: str) -> dict:
    resp = requests.get(
        FINMIND,
        params={"dataset": dataset, "data_id": data_id, "start_date": start, "token": token},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def parse_finmind_close(payload: dict) -> float | None:
    data = payload.get("data") or []
    return data[-1].get("close") if data else None
