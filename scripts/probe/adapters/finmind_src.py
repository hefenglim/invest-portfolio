# mypy: ignore-errors
"""FinMind (keyed) TW price/fx/dividend + the spec-20 Free-tier chips datasets."""

import os

import requests

FINMIND = "https://api.finmindtrade.com/api/v4/data"

# spec-20.6 Free-tier datasets the chips/fundamental ingest exercises.
FINMIND_DATASETS = {
    "institutional": "TaiwanStockInstitutionalInvestorsBuySell",
    "margin": "TaiwanStockMarginPurchaseShortSale",
    "valuation": "TaiwanStockPER",
    "monthly_revenue": "TaiwanStockMonthRevenue",
    "financials": "TaiwanStockFinancialStatements",
}


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


def fetch_finmind_dataset(name: str, data_id: str, start: str, token: str) -> dict:
    """Fetch one logical chips dataset (mapped via FINMIND_DATASETS)."""
    return fetch_finmind(FINMIND_DATASETS[name], data_id, start, token)


def parse_finmind_close(payload: dict) -> float | None:
    data = payload.get("data") or []
    return data[-1].get("close") if data else None


def parse_dataset_rows(payload: dict) -> list[dict]:
    """Return the raw ``data`` rows for any FinMind dataset (empty list if absent)."""
    data = payload.get("data")
    return data if isinstance(data, list) else []
