"""FinMind Free-tier multi-dataset client (spec 20.6 + 20.15).

A light, single-source client for the chips/fundamental datasets that feed the
external-snapshot ingest (spec 20.4). Unlike ``providers/finmind_provider.py``
(which produces dividend *numbers of record* through the registry), this client
returns the **raw** FinMind ``data`` list verbatim — Decimal conversion happens
later in the derivation layer (``portfolio/external_signals.py``).

Auth (spec 20.15.1): the token is read at call time from the ``data_sources`` table
(spec 14.2) via ``datasources_store.get_api_key`` and sent as an
``Authorization: Bearer {token}`` header (the official scheme; not a ``?token=``
query param); a missing key raises ``MissingTokenError`` before any network call.

Tier/quota guards (spec 20.15.4): every dataset's required tier is in
:data:`DATASET_TIER`. A LOCAL preflight compares it against the token's marked tier
(``data_sources.tier``) and raises :class:`FinMindTierError` WITHOUT a network call
when the token is too low (saves quota, clear message). A response that signals the
600/hr quota is exhausted (HTTP 402 or JSON ``status == 402``) raises
:class:`FinMindQuotaError`. All HTTP I/O goes through ``requests.get`` so tests can
monkeypatch it (the repo bans sockets in tests).
"""

import sqlite3
from typing import Any

import requests

from portfolio_dash.pricing import datasources_store
from portfolio_dash.pricing.datasources_store import TIER_ORDER

_URL = "https://api.finmindtrade.com/api/v4/data"
_USER_INFO_URL = "https://api.web.finmindtrade.com/v2/user_info"
_TIMEOUT_S = 20
_QUOTA_STATUS = 402

# Logical dataset name -> FinMind dataset id (Free tier; spec 20.6).
FINMIND_DATASETS: dict[str, str] = {
    "institutional": "TaiwanStockInstitutionalInvestorsBuySell",
    "margin": "TaiwanStockMarginPurchaseShortSale",
    "valuation": "TaiwanStockPER",
    "monthly_revenue": "TaiwanStockMonthRevenue",
    "financials": "TaiwanStockFinancialStatements",
}

# Logical dataset name -> required token tier *for our query mode* (spec 20.15.2).
# We ALWAYS pass ``data_id`` (per-stock), so every dataset stays on the Free tier.
DATASET_TIER: dict[str, str] = {name: "free" for name in FINMIND_DATASETS}


class MissingTokenError(RuntimeError):
    """Raised when no FinMind API key is configured for a dataset fetch."""


class FinMindQuotaError(RuntimeError):
    """Raised when FinMind signals the request quota is exhausted (HTTP/JSON 402)."""


class FinMindTierError(RuntimeError):
    """Raised when the dataset's required tier exceeds the token's marked tier.

    Carries ``required_tier`` so callers (ingest, panel) can phrase a precise message
    (e.g. "需要 Backer 方案").
    """

    def __init__(self, message: str, *, required_tier: str) -> None:
        super().__init__(message)
        self.required_tier = required_tier


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _tier_rank(tier: str | None) -> int:
    """Rank a tier string; an unknown/unset tier is the lowest effective rank (free)."""
    if tier is None:
        return TIER_ORDER["free"]
    return TIER_ORDER.get(tier, TIER_ORDER["free"])


def _check_quota(payload: dict[str, Any], *, http_status: int) -> None:
    """Raise :class:`FinMindQuotaError` if the response signals quota exhaustion."""
    json_status = payload.get("status")
    if http_status == _QUOTA_STATUS or json_status == _QUOTA_STATUS:
        msg = payload.get("msg") or "FinMind request quota exhausted"
        raise FinMindQuotaError(str(msg))


def fetch_dataset(
    conn: sqlite3.Connection,
    *,
    dataset: str,
    data_id: str,
    start_date: str,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch one FinMind dataset's raw ``data`` rows for a symbol.

    ``dataset`` is a logical key in :data:`FINMIND_DATASETS` (``KeyError`` if unknown).
    Resolves the token from the DB and runs a LOCAL tier preflight (no network when the
    token tier is below the dataset's required tier → :class:`FinMindTierError`). Sends
    Bearer auth; optional ``end_date`` bounds the date range (saves quota). A quota body
    (HTTP/JSON 402) raises :class:`FinMindQuotaError`. The returned list is the
    provider's raw ``data`` (no Decimal coercion here).
    """
    finmind_id = FINMIND_DATASETS[dataset]
    token = datasources_store.get_api_key(conn, "finmind")
    if not token:
        raise MissingTokenError("FinMind API key is not configured")
    # Local tier preflight (spec 20.15.4): never spend a request when the token can't
    # access this dataset under our query mode.
    required = DATASET_TIER[dataset]
    token_tier = datasources_store.get_tier(conn, "finmind")
    if _tier_rank(required) > _tier_rank(token_tier):
        raise FinMindTierError(
            f"FinMind dataset {dataset!r} requires the {required} tier", required_tier=required
        )
    params: dict[str, Any] = {
        "dataset": finmind_id,
        "data_id": data_id,
        "start_date": start_date,
    }
    if end_date is not None:
        params["end_date"] = end_date
    resp = requests.get(
        _URL, params=params, headers=_bearer(token), timeout=_TIMEOUT_S
    )
    payload = resp.json()
    _check_quota(payload, http_status=resp.status_code)
    resp.raise_for_status()
    data = payload.get("data")
    return data if isinstance(data, list) else []


def fetch_quota(conn: sqlite3.Connection) -> dict[str, int]:
    """Fetch the token's usage / limit from FinMind ``user_info`` (spec 20.15.5).

    Returns ``{"user_count", "api_request_limit"}``. The limit reveals the tier
    (600=free, 1600=backer, 6000=sponsor, 20000=sponsorpro). Bearer auth; missing token
    → :class:`MissingTokenError` before any network call.
    """
    token = datasources_store.get_api_key(conn, "finmind")
    if not token:
        raise MissingTokenError("FinMind API key is not configured")
    resp = requests.get(_USER_INFO_URL, headers=_bearer(token), timeout=_TIMEOUT_S)
    payload = resp.json()
    _check_quota(payload, http_status=resp.status_code)
    resp.raise_for_status()
    return {
        "user_count": int(payload.get("user_count", 0)),
        "api_request_limit": int(payload.get("api_request_limit", 0)),
    }
