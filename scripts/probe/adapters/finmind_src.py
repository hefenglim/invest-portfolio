# mypy: ignore-errors
"""FinMind (keyed) TW price/fx/dividend + the spec-20 Free-tier chips datasets.

Auth follows the production client (spec 20.15.1): ``Authorization: Bearer {token}``
(not a ``?token=`` query param). ``fetch_quota`` reports usage/limit so the probe can
note the token's tier (600=free, 1600=backer, 6000=sponsor, 20000=sponsorpro).
"""

import os

import requests

FINMIND = "https://api.finmindtrade.com/api/v4/data"
FINMIND_USER_INFO = "https://api.web.finmindtrade.com/v2/user_info"

# spec-20.6 Free-tier datasets the chips/fundamental ingest exercises.
FINMIND_DATASETS = {
    "institutional": "TaiwanStockInstitutionalInvestorsBuySell",
    "margin": "TaiwanStockMarginPurchaseShortSale",
    "valuation": "TaiwanStockPER",
    "monthly_revenue": "TaiwanStockMonthRevenue",
    "financials": "TaiwanStockFinancialStatements",
}

# Required tier per logical dataset under our always-data_id query mode (spec 20.15.2):
# all 5 stay Free. api_request_limit -> tier reveal for the quota note.
DATASET_TIER = {name: "free" for name in FINMIND_DATASETS}
LIMIT_TO_TIER = {600: "free", 1600: "backer", 6000: "sponsor", 20000: "sponsorpro"}


def finmind_token() -> str | None:
    return os.environ.get("FINMIND_TOKEN")


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def fetch_finmind(dataset: str, data_id: str, start: str, token: str) -> dict:
    resp = requests.get(
        FINMIND,
        params={"dataset": dataset, "data_id": data_id, "start_date": start},
        headers=_bearer(token),
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_quota(token: str) -> dict:
    """Report FinMind ``user_info`` usage/limit (Bearer auth) for the probe tier note."""
    resp = requests.get(FINMIND_USER_INFO, headers=_bearer(token), timeout=20)
    resp.raise_for_status()
    return resp.json()


def tier_from_limit(limit) -> str | None:
    """Infer the token's tier from ``api_request_limit`` (None if unrecognized)."""
    try:
        return LIMIT_TO_TIER.get(int(limit))
    except (TypeError, ValueError):
        return None


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
