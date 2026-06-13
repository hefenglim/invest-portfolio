"""strategy/ HTTP routes (spec 03): alert-rules config (this task), then alerts/whatif/
rebalance (later tasks). Thin: reads/writes config + calls the strategy core."""

import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.api.errors import error_body
from portfolio_dash.api.serialize import to_wire
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.strategy.alerts import compute_alerts
from portfolio_dash.strategy.rules_config import (
    RULE_IDS,
    RULE_META,
    get_alert_rules,
    set_alert_rules,
)
from portfolio_dash.strategy.whatif import WhatIfError, compute_whatif

router = APIRouter()


def _rules_wire(rules: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rid in RULE_IDS:
        rule = getattr(rules, rid)
        _dv, unit, mn, mx = RULE_META[rid]
        out.append({
            "id": rid, "enabled": rule.enabled,
            "value": None if rule.value is None else str(rule.value),
            "unit": unit, "min": mn, "max": mx,
        })
    return out


@router.get("/alert-rules")
def get_rules(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    return {"rules": _rules_wire(get_alert_rules(conn))}


@router.get("/alerts")
def get_alerts(conn: sqlite3.Connection = Depends(get_conn),
               now: datetime = Depends(get_now),
               reporting: Currency = Depends(get_reporting)) -> dict[str, Any]:
    alerts = compute_alerts(conn, now=now, reporting=reporting)
    return {"as_of": now.isoformat(), "alerts": to_wire([a.model_dump() for a in alerts])}


class RuleInput(BaseModel):
    id: str
    enabled: bool
    value: str | None = None


class AlertRulesBody(BaseModel):
    rules: list[RuleInput]


@router.put("/alert-rules")
def put_rules(body: AlertRulesBody,
              conn: sqlite3.Connection = Depends(get_conn),
              now: datetime = Depends(get_now),
              reporting: Currency = Depends(get_reporting)) -> Any:
    current = get_alert_rules(conn)
    for item in body.rules:
        if item.id not in RULE_META:
            return JSONResponse(status_code=400, content=error_body(
                "validation_error", f"未知規則 {item.id}", field="id"))
        _dv, _unit, mn, mx = RULE_META[item.id]
        value: Decimal | None = None
        if item.value is not None:
            try:
                value = Decimal(item.value)
            except InvalidOperation:
                return JSONResponse(status_code=400, content=error_body(
                    "validation_error", f"{item.id} 數值無效", field="value"))
            if mn is not None and value < Decimal(mn):
                return JSONResponse(status_code=400, content=error_body(
                    "validation_error", f"{item.id} 低於下限", field="value"))
            if mx is not None and value > Decimal(mx):
                return JSONResponse(status_code=400, content=error_body(
                    "validation_error", f"{item.id} 高於上限", field="value"))
        rule = getattr(current, item.id)
        rule.enabled = item.enabled
        # toggle-only rules (value meta = None) ignore any submitted value
        if _dv is not None:
            rule.value = value
    set_alert_rules(conn, current)
    alerts = compute_alerts(conn, now=now, reporting=reporting)
    return {"rules": _rules_wire(current),
            "alerts": to_wire([a.model_dump() for a in alerts])}


class WhatIfBody(BaseModel):
    symbol: str
    side: str  # "buy" | "sell"
    shares: Decimal
    price: Decimal
    account_id: str | None = None


@router.post("/whatif")
def post_whatif(body: WhatIfBody,
                conn: sqlite3.Connection = Depends(get_conn),
                now: datetime = Depends(get_now),
                reporting: Currency = Depends(get_reporting)) -> Any:
    try:
        side = Side(body.side.strip().upper())
    except ValueError:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"未知交易方向 {body.side}", field="side"))
    try:
        result = compute_whatif(conn, now=now, reporting=reporting, symbol=body.symbol,
                                side=side, shares=body.shares, price=body.price,
                                account_id=body.account_id)
    except WhatIfError as exc:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", str(exc), field="account_id"))
    return result
