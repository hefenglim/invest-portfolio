"""strategy/ HTTP routes (spec 03): alert-rules config (this task), then alerts/whatif/
rebalance (later tasks). Thin: reads/writes config + calls the strategy core."""

import sqlite3
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from portfolio_dash.api.deps import get_conn
from portfolio_dash.api.errors import error_body
from portfolio_dash.strategy.rules_config import (
    RULE_IDS,
    RULE_META,
    get_alert_rules,
    set_alert_rules,
)

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


class RuleInput(BaseModel):
    id: str
    enabled: bool
    value: str | None = None


class AlertRulesBody(BaseModel):
    rules: list[RuleInput]


@router.put("/alert-rules")
def put_rules(body: AlertRulesBody,
              conn: sqlite3.Connection = Depends(get_conn)) -> Any:
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
    return {"rules": _rules_wire(current)}
