"""Input center API (spec 12): read context + manual/CSV/AI write paths (12a: context+manual)."""

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends

from portfolio_dash.api.deps import get_conn
from portfolio_dash.api.wire import div_model_wire, fee_rules_wire
from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set
from portfolio_dash.data_ingestion.holdings import current_shares
from portfolio_dash.data_ingestion.store import list_accounts, list_instruments

router = APIRouter()


@router.get("/input/context")
def context(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT account_id, fee_rule_set, dividend_model FROM accounts ORDER BY account_id"
    ).fetchall()
    meta = {r["account_id"]: r for r in rows}
    accts = list_accounts(conn)
    accounts_out = [
        {
            "id": a.account_id,
            "name": a.name,
            "ccy": a.settlement_ccy.value,
            "div_model": div_model_wire(meta[a.account_id]["dividend_model"]),
        }
        for a in accts
    ]
    fee_rules = {
        aid: fee_rules_wire(get_fee_rule_set(m["fee_rule_set"])) for aid, m in meta.items()
    }
    insts = list_instruments(conn)
    instruments = [
        {
            "symbol": i.symbol,
            "name": i.name,
            "market": i.market.value,
            "ccy": i.quote_ccy.value,
            "etf": i.is_etf,
        }
        for i in insts
    ]
    holdings: dict[str, dict[str, str]] = {}
    for a in accts:
        per = {
            inst.symbol: str(sh)
            for inst in insts
            if (sh := current_shares(conn, a.account_id, inst.symbol)) != 0
        }
        if per:
            holdings[a.account_id] = per
    return {
        "accounts": accounts_out,
        "fee_rules": fee_rules,
        "instruments": instruments,
        "holdings": holdings,
    }
