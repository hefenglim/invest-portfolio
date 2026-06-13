"""Accounts & fees API (spec 13): GET /api/accounts — read-only.

Four broker accounts + dividend model + full fee-rule set, plus the rule-set
version (settings_meta seeded time). Thin over ``store.list_accounts`` +
``config_seed.get_fee_rule_set`` and the shared ``wire`` serializers. Reads only;
computes nothing of record.
"""

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends

from portfolio_dash.api.deps import get_conn
from portfolio_dash.api.wire import div_model_wire, fee_rules_wire
from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set
from portfolio_dash.data_ingestion.store import list_accounts

router = APIRouter()

_CATEGORY = "accounts"


def _seeded_at(conn: sqlite3.Connection) -> str | None:
    """Rule-set version = settings_meta seeded time for the ``accounts`` category.

    ``seed_accounts`` writes the accounts table directly and does not register an
    ``accounts`` category in ``settings_meta``; absent a real seed time we return
    ``None`` rather than fabricate one.
    """
    row = conn.execute(
        "SELECT seeded_at FROM settings_meta WHERE category = ?", (_CATEGORY,)
    ).fetchone()
    return row["seeded_at"] if row is not None else None


@router.get("/accounts")
def list_all(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    meta = {
        r["account_id"]: r
        for r in conn.execute(
            "SELECT account_id, fee_rule_set, dividend_model FROM accounts"
        ).fetchall()
    }
    accounts_out = [
        {
            "account_id": a.account_id,
            "name": a.name,
            "broker": a.broker,
            "settlement_ccy": a.settlement_ccy.value,
            "funding_ccy": a.funding_ccy.value,
            "div_model": div_model_wire(meta[a.account_id]["dividend_model"]),
            "fee_rules": fee_rules_wire(get_fee_rule_set(meta[a.account_id]["fee_rule_set"])),
        }
        for a in list_accounts(conn)
    ]
    return {
        "version": {"category": _CATEGORY, "seeded_at": _seeded_at(conn)},
        "accounts": accounts_out,
    }
