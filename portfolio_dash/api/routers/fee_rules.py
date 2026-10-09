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
from portfolio_dash.data_ingestion.fee_overrides import (
    DISPLAY_FIELD_ORDER,
    FeeOverrideError,
    is_editable,
)
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


_ONE = Decimal("1")
_ZERO = Decimal("0")


def _conflicts(rs: FeeRuleSet) -> list[dict[str, Any]]:
    """Settings that are individually legal but describe the SAME benefit twice.

    ``discount`` and ``rebate_rate`` are two ways to record one broker discount: charge less
    now, or charge full and refund later (FE-D1, the 群益 先收後退 model). Turning BOTH on
    applies it twice — and because fees are part of cost basis, that quietly understates
    every affected position. Measured on the test site 2026-07-30: a TW trade whose full
    commission is 869 was charged 199 AND forecast a 153 refund, i.e. a net 46 instead of the
    intended 200.

    This is a WARNING, never a block: a broker really could do both, and only the owner
    knows. The response carries the plain-language explanation so the UI never has to
    invent one, and the owner can either accept it knowingly or revert.
    """
    out: list[dict[str, Any]] = []
    discount = getattr(rs, "discount", None)
    rebate = getattr(rs, "rebate_rate", None)
    if (isinstance(discount, Decimal) and isinstance(rebate, Decimal)
            and discount < _ONE and rebate > _ZERO):
        # 100 -> charged 100*discount -> refunded that * rebate -> what is actually paid.
        charged = (Decimal("100") * discount).quantize(Decimal("0.01"))
        refund = (charged * rebate).quantize(Decimal("0.01"))
        out.append({
            "fields": ["discount", "rebate_rate"],
            "title": "折扣被算了兩次",
            "plain": (
                "把手續費想成餐廳的兩種折扣寫法，兩種都是「打完折你付一樣的錢」：\n"
                "　A 結帳直接打折 —— 原價 100 元，收你 "
                f"{decimal_str(charged)} 元。（這是「折扣率」）\n"
                "　B 先付全額、下個月退 —— 原價 100 元先收 100 元，次月退你回來。"
                "（這是「折讓款比例」）\n"
                "你的券商只會用其中一種。現在兩種同時開著，變成："
                f"結帳先收 {decimal_str(charged)} 元，下個月又退 {decimal_str(refund)} 元，"
                f"最後只付了 {decimal_str(charged - refund)} 元 —— 同一個折扣打了兩次。\n"
                "手續費會算進持股成本，所以這會讓成本偏低、報酬率看起來偏高。"
            ),
            "options": [
                {"label": "券商是「先收全額、次月退款」（群益屬此）",
                 "set": {"discount": "1"}},
                {"label": "券商是「結帳當下就打折」（沒有退款）",
                 "set": {"rebate_rate": "0"}},
            ],
        })
    return out


def _rule_set_wire(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    base = FEE_RULES[name]
    effective = get_fee_rule_set(name, conn)
    overlay = fee_overrides.overlay_for(conn, name)
    overridden = overlay.fields if overlay is not None else {}
    # DISPLAY, not EDITABLE: ``rounding`` stopped being writable (QA-07 — the engine never
    # read it, yet it was stamped into every row's permanent fee_rule_snapshot as the regime
    # that produced the numbers). Dropping it from the editable set alone would have made it
    # INVISIBLE, which is a different wrong answer: TW's 無條件捨去 is FE-D3, 財政部 角以下
    # 免收 — a market fact the owner should be able to SEE and not change. The ``editable``
    # flag is what lets the frontend tell "you may not change this" from "this is not here".
    fields = [
        {
            "key": key,
            "default": _field_value(base, key),
            "effective": _field_value(effective, key),
            "overridden": key in overridden,
            "editable": is_editable(key),
        }
        for key in DISPLAY_FIELD_ORDER
    ]
    return {
        "name": name,
        "market": base.market.value,
        "updated_at": overlay.updated_at if overlay is not None else None,
        "fields": fields,
        # Non-blocking: a setting the owner may knowingly keep. Computed from the EFFECTIVE
        # values, so it appears on GET too — an override made before this check existed is
        # surfaced the next time the page is opened, not only when it is edited.
        "conflicts": _conflicts(effective),
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
