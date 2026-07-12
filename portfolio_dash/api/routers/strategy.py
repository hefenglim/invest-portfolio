"""strategy/ HTTP routes (spec 03): alert-rules config (this task), then alerts/whatif/
rebalance (later tasks). Thin: reads/writes config + calls the strategy core."""

import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from portfolio_dash.api import alert_inputs, insight_service
from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.api.errors import error_body
from portfolio_dash.api.serialize import to_wire
from portfolio_dash.data_ingestion.holdings import current_shares
from portfolio_dash.data_ingestion.store import list_accounts, list_instruments
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.wire import decimal_str
from portfolio_dash.strategy import target_weights as tw
from portfolio_dash.strategy.rebalance import compute_rebalance
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
            "value": None if rule.value is None else decimal_str(rule.value),
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
    # calib_gap fed from the single-source helper so this matches the dashboard embed.
    # calibration_regression (the spec-04c event) stays in alert_events and is NOT here (W3).
    # compute_alerts_full assembles the SAME P3 market-risk inputs as the dashboard embed.
    calib = insight_service.calibration_gap(conn)
    alerts = alert_inputs.compute_alerts_full(conn, now=now, reporting=reporting, calib_gap=calib)
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
    # calib_gap fed from the single-source helper (matches the dashboard embed + GET /alerts).
    # calibration_regression (the spec-04c event) stays in alert_events and is NOT here (W3).
    calib = insight_service.calibration_gap(conn)
    alerts = alert_inputs.compute_alerts_full(conn, now=now, reporting=reporting, calib_gap=calib)
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


class RebalanceBody(BaseModel):
    targets: dict[str, Decimal]


@router.post("/rebalance/preview")
def post_rebalance(body: RebalanceBody,
                   conn: sqlite3.Connection = Depends(get_conn),
                   now: datetime = Depends(get_now),
                   reporting: Currency = Depends(get_reporting)) -> Any:
    for symbol, ratio in body.targets.items():
        if ratio < Decimal("0"):
            return JSONResponse(status_code=400, content=error_body(
                "validation_error", f"{symbol} 目標權重不可為負", field="targets"))
    result = compute_rebalance(conn, now=now, reporting=reporting, targets=body.targets)
    return to_wire(result)


# --- Target weights config (D8): the single source for rule ③ + rebalance prefill --------

_ZERO = Decimal("0")
_ONE = Decimal("1")


def _held_set(conn: sqlite3.Connection) -> set[str]:
    """Symbols carrying a live position in any account (cheap net-shares check)."""
    account_ids = [a.account_id for a in list_accounts(conn)]
    return {
        inst.symbol
        for inst in list_instruments(conn)
        if any(current_shares(conn, aid, inst.symbol) > _ZERO for aid in account_ids)
    }


def _target_weights_view(conn: sqlite3.Connection) -> dict[str, Any]:
    """The GET/PUT response: one row per REGISTERED symbol + held/watch flag + Σ."""
    tw.ensure_target_weights_seeded(conn)
    stored = tw.load_target_weights(conn)
    held = _held_set(conn)
    total = _ZERO
    rows: list[dict[str, Any]] = []
    for inst in sorted(list_instruments(conn), key=lambda i: i.symbol):
        w = stored.get(inst.symbol)
        if w is not None:
            total += w
        rows.append({
            "symbol": inst.symbol, "name": inst.name, "held": inst.symbol in held,
            "weight": None if w is None else decimal_str(w),
        })
    return {"symbols": rows, "sum": decimal_str(total),
            "updated_at": tw.get_updated_at(conn)}


@router.get("/target-weights")
def get_target_weights(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    return _target_weights_view(conn)


class TargetWeightsBody(BaseModel):
    weights: dict[str, str]  # symbol -> Decimal-string ratio; absent/empty = unset


@router.put("/target-weights")
def put_target_weights(body: TargetWeightsBody,
                       conn: sqlite3.Connection = Depends(get_conn),
                       now: datetime = Depends(get_now)) -> Any:
    """Validate + persist the target weights (ratios). 400 on unknown symbol / bad value /
    each ∉ (0,1] / Σ > 1 — a zh message. An empty map clears all targets (valid)."""
    registered = {i.symbol for i in list_instruments(conn)}
    weights: dict[str, Decimal] = {}
    total = _ZERO
    for sym, raw in body.weights.items():
        if sym not in registered:
            return JSONResponse(status_code=400, content=error_body(
                "validation_error", f"未知標的 {sym}", field="weights"))
        try:
            val = Decimal(raw)
        except InvalidOperation:
            return JSONResponse(status_code=400, content=error_body(
                "validation_error", f"{sym} 權重數值無效", field="weights"))
        if val <= _ZERO or val > _ONE:
            return JSONResponse(status_code=400, content=error_body(
                "validation_error", f"{sym} 目標權重須介於 0% 與 100%", field="weights"))
        weights[sym] = val
        total += val
    if total > _ONE:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "目標權重合計不可超過 100%", field="weights"))
    tw.save_target_weights(conn, weights, now=now)
    return _target_weights_view(conn)
