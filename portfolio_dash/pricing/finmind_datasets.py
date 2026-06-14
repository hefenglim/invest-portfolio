"""FinMind Free-tier multi-dataset client (spec 20.6).

A light, single-source client for the chips/fundamental datasets that feed the
external-snapshot ingest (spec 20.4). Unlike ``providers/finmind_provider.py``
(which produces dividend *numbers of record* through the registry), this client
returns the **raw** FinMind ``data`` list verbatim — Decimal conversion happens
later in the derivation layer (``portfolio/external_signals.py``).

Token is read at call time from the ``data_sources`` table (spec 14.2) via
``datasources_store.get_api_key``; a missing key raises ``MissingTokenError``
before any network call. All HTTP I/O goes through ``requests.get`` so tests can
monkeypatch it (the repo bans sockets in tests).
"""

import sqlite3
from typing import Any

import requests

from portfolio_dash.pricing import datasources_store

_URL = "https://api.finmindtrade.com/api/v4/data"
_TIMEOUT_S = 20

# Logical dataset name -> FinMind dataset id (Free tier; spec 20.6).
FINMIND_DATASETS: dict[str, str] = {
    "institutional": "TaiwanStockInstitutionalInvestorsBuySell",
    "margin": "TaiwanStockMarginPurchaseShortSale",
    "valuation": "TaiwanStockPER",
    "monthly_revenue": "TaiwanStockMonthRevenue",
    "financials": "TaiwanStockFinancialStatements",
}


class MissingTokenError(RuntimeError):
    """Raised when no FinMind API key is configured for a dataset fetch."""


def fetch_dataset(
    conn: sqlite3.Connection, *, dataset: str, data_id: str, start_date: str
) -> list[dict[str, Any]]:
    """Fetch one FinMind dataset's raw ``data`` rows for a symbol.

    ``dataset`` is a logical key in :data:`FINMIND_DATASETS` (``KeyError`` if unknown).
    Resolves the token from the DB; raises :class:`MissingTokenError` if unset. The
    returned list is the provider's raw ``data`` (no Decimal coercion here).
    """
    finmind_id = FINMIND_DATASETS[dataset]
    token = datasources_store.get_api_key(conn, "finmind")
    if not token:
        raise MissingTokenError("FinMind API key is not configured")
    resp = requests.get(
        _URL,
        params={
            "dataset": finmind_id,
            "data_id": data_id,
            "start_date": start_date,
            "token": token,
        },
        timeout=_TIMEOUT_S,
    )
    resp.raise_for_status()
    payload = resp.json()
    data = payload.get("data")
    return data if isinstance(data, list) else []
