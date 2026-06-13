"""End-to-end LLM quota accounting reconciliation (senior-review finding I-1).

Proves the unified topup-cumulative budget model — ``remaining = Σ top-ups − Σ usage``
— reconciles across ALL readers with no gaps, driven through the REAL app:

* the gate         (``check_budget`` / ``budget_remaining`` in ``shared/llm_config``),
* the settings page (``GET /api/llm/config`` → ``quota.remaining_usd``),
* the dashboard chip (``GET /api/dashboard`` → ``llm_quota.remaining_usd``),
* the spec-16 alias  (``quota_remaining`` delegating to ``budget_remaining``),

are one and the same number at every step, and exhaustion (``check_budget`` raising
``LLMBudgetExceeded``) coincides exactly with ``Σ top-ups == Σ usage``.

Money-string note: the quota endpoints emit ``str(Decimal)`` verbatim. A JSON *number*
``10.00`` reaches FastAPI as a float and serializes back as ``"10.0"``; a JSON *string*
``"10.00"`` round-trips exactly as ``"10.00"``. This test tops up with string amounts
(the money-correct form) so the asserted strings are the clean ``"10.00"`` family.
"""

import sqlite3
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.shared.llm import log_usage
from portfolio_dash.shared.llm_config import (
    LLMBudgetExceeded,
    budget_remaining,
    check_budget,
    quota_remaining,
)


def _settings_remaining(client: TestClient) -> str:
    """The settings-page reader: GET /api/llm/config → quota.remaining_usd."""
    resp = client.get("/api/llm/config")
    assert resp.status_code == 200, resp.text
    return str(resp.json()["quota"]["remaining_usd"])


def _dashboard_remaining(client: TestClient) -> str:
    """The dashboard-chip reader: GET /api/dashboard → llm_quota.remaining_usd."""
    resp = client.get("/api/dashboard")
    assert resp.status_code == 200, resp.text
    return str(resp.json()["llm_quota"]["remaining_usd"])


def _assert_all_agree(
    client: TestClient, conn: sqlite3.Connection, expected: str
) -> None:
    """The crux: gate == settings == dashboard == spec-16 alias, one number, no gaps."""
    gate = str(budget_remaining(conn))
    alias = str(quota_remaining(conn))
    settings = _settings_remaining(client)
    dashboard = _dashboard_remaining(client)
    assert gate == expected, f"gate (budget_remaining) = {gate!r}, expected {expected!r}"
    assert alias == expected, f"quota_remaining = {alias!r}, expected {expected!r}"
    assert settings == expected, f"GET /api/llm/config = {settings!r}, expected {expected!r}"
    assert dashboard == expected, f"GET /api/dashboard = {dashboard!r}, expected {expected!r}"


def _topup(client: TestClient, amount: str) -> str:
    """Drive a top-up through the API; return its echoed remaining_usd string."""
    resp = client.post("/api/llm/quota/topup", json={"amount_usd": amount})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["remaining_usd"])


def test_quota_accounting_reconciles_across_all_readers(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """topup → usage → remaining, asserting gate==settings==dashboard at every step,
    plus the Σtopups − Σusage == remaining identity and exhaustion at Σtopups == Σusage.
    """
    conn = golden_db
    sum_topups = Decimal("0")
    sum_usage = Decimal("0")

    # 1. Fresh golden_db: nothing funded, nothing spent → all readers agree at $0.
    _assert_all_agree(api_client, conn, "0")
    check_budget_raised = False
    try:
        check_budget(conn)  # $0 funded → already exhausted
    except LLMBudgetExceeded:
        check_budget_raised = True
    assert check_budget_raised, "fresh $0 budget must block (remaining <= 0)"

    # 2. Top up $10.00 through the API → remaining 10.00 across all readers.
    assert _topup(api_client, "10.00") == "10.00"
    sum_topups += Decimal("10.00")
    _assert_all_agree(api_client, conn, "10.00")
    assert (sum_topups - sum_usage) == budget_remaining(conn)

    # 3. Log $4.00 of usage → remaining 6.00 across all readers.
    log_usage(
        conn, model="claude-sonnet-4-5", agent="insight_composer",
        input_tokens=1000, output_tokens=500, cost=Decimal("4.00"),
    )
    sum_usage += Decimal("4.00")
    _assert_all_agree(api_client, conn, "6.00")
    assert (sum_topups - sum_usage) == budget_remaining(conn)

    # 4. Top up $5.00 (cumulative) → remaining 11.00 — proves top-ups ADD, never reset.
    assert _topup(api_client, "5.00") == "11.00"
    sum_topups += Decimal("5.00")
    _assert_all_agree(api_client, conn, "11.00")
    assert (sum_topups - sum_usage) == budget_remaining(conn)

    # 5. Consume the remaining 11.00 → remaining 0.00; exhausted, the gate now blocks.
    log_usage(
        conn, model="claude-sonnet-4-5", agent="insight_composer",
        input_tokens=2750, output_tokens=1375, cost=Decimal("11.00"),
    )
    sum_usage += Decimal("11.00")
    _assert_all_agree(api_client, conn, "0.00")

    # Accounting identity at the floor: Σ top-ups (15.00) == Σ usage (15.00).
    assert sum_topups == Decimal("15.00")
    assert sum_usage == Decimal("15.00")
    assert sum_topups == sum_usage
    assert budget_remaining(conn) == (sum_topups - sum_usage) == Decimal("0.00")

    # Exhaustion coincides with Σtopups == Σusage: the gate raises again.
    with pytest.raises(LLMBudgetExceeded):
        check_budget(conn)
