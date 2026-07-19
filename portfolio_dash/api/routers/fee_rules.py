"""Fee-rule center API (FU-D1 / FU-D2): read + edit the fee-rule overlay over config_seed v2.

The effective rule set = ``config_seed.FEE_RULES`` (fee-engine v2 defaults) merged with a
per-field DB overlay (:mod:`data_ingestion.fee_overrides`). Editing here affects FUTURE fee/tax
computations only — every transaction row keeps its own ``fee_rule_snapshot``, so history is
never recomputed.

Gate: session gate only (FU-D1 — open in guest mode, same class as scheduler / ledger config;
no outbound side effects, and reset makes a demo recoverable). NO ``is_protected`` 403.
"""

import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.errors import error_body
from portfolio_dash.data_ingestion import fee_overrides
from portfolio_dash.data_ingestion.config_seed import FEE_RULES, FeeRuleSet, get_fee_rule_set
from portfolio_dash.data_ingestion.fee_overrides import EDITABLE_FIELD_ORDER, FeeOverrideError
from portfolio_dash.shared.wire import decimal_str

router = APIRouter()


def _field_value(rs: FeeRuleSet, key: str) -> str | None:
    """Serialize one field: a Decimal -> canonical string; a null cap -> null; enum -> str."""
    val = getattr(rs, key)
    if val is None:
        return None
    if isinstance(val, Decimal):
        return decimal_str(val)
    return str(val)  # rounding literal ("floor" / "half_up")


def _rule_set_wire(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    base = FEE_RULES[name]
    effective = get_fee_rule_set(name, conn)
    overlay = fee_overrides.overlay_for(conn, name)
    overridden = overlay.fields if overlay is not None else {}
    fields = [
        {
            "key": key,
            "default": _field_value(base, key),
            "effective": _field_value(effective, key),
            "overridden": key in overridden,
        }
        for key in EDITABLE_FIELD_ORDER
    ]
    return {
        "name": name,
        "market": base.market.value,
        "updated_at": overlay.updated_at if overlay is not None else None,
        "fields": fields,
    }


@router.get("/fee-rules")
def list_fee_rules(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """Every rule set: per-field default + effective + overridden flag (Decimal strings)."""
    return {"rule_sets": [_rule_set_wire(conn, name) for name in FEE_RULES]}


class FeeRulePutBody(BaseModel):
    """A batch of field changes: value-string = set, ``null`` = revert that field to default."""

    overrides: dict[str, Any]


@router.put("/fee-rules/{name}")
def update_fee_rule(
    name: str,
    body: FeeRulePutBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    if name not in FEE_RULES:
        return JSONResponse(status_code=404, content=error_body(
            "not_found", f"費率規則不存在：{name}"))
    try:
        fee_overrides.set_overrides(conn, name, body.overrides, now=now)
    except FeeOverrideError as exc:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", str(exc), field=exc.field))
    return _rule_set_wire(conn, name)


@router.post("/fee-rules/reset-all")
def reset_all_fee_rules(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """Delete every overlay row — revert all rule sets to fee-engine v2 defaults."""
    fee_overrides.reset_all(conn)
    return {"rule_sets": [_rule_set_wire(conn, name) for name in FEE_RULES]}


@router.post("/fee-rules/{name}/reset")
def reset_fee_rule(
    name: str, conn: sqlite3.Connection = Depends(get_conn)
) -> Any:
    """Delete one rule set's overlay — revert every field to its fee-engine v2 default."""
    if name not in FEE_RULES:
        return JSONResponse(status_code=404, content=error_body(
            "not_found", f"費率規則不存在：{name}"))
    fee_overrides.reset(conn, name)
    return _rule_set_wire(conn, name)


__all__ = ["router"]
